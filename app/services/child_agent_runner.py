"""子代理执行服务。

提供 ChildAgentRunner，负责执行 child profile 并把执行过程中的
历史消息隔离写入 child context，维护 child run 的完整生命周期。
"""

from __future__ import annotations  # 启用未来注解，避免前向引用问题

import asyncio  # 导入异步工具，用于 cancel_event 默认值
from dataclasses import dataclass  # 导入数据类装饰器，用于 ChildAgentRunResult
from datetime import datetime, timezone  # 导入日期时间类，用于时间戳
import uuid  # 导入 UUID 模块，用于生成 child run ID

from app.config import Settings  # 导入应用配置
from app.core.loop.agent_loop import AgentLoop  # 导入 AgentLoop 编排器
from app.core.models.agent import Agent, AgentExecutionProfile  # 导入执行配置类型
from app.core.models.error import ErrorCode  # 导入错误码枚举
from app.core.models.tool import Tool, ToolRegistry  # 导入工具模型和注册表
from app.infra.agents.profile_builder import CHILD_FILTERED_TOOL_NAMES  # 导入主控工具名称集合，用于动态过滤
from app.core.models.event import (  # 导入事件模型
    AssistantWithToolsEvent,  # 带工具调用的 assistant 消息事件
    MessageDeltaEvent,  # 流式文本增量事件
    RunCancelledEvent,  # 运行取消事件
    RunCompletedEvent,  # 运行完成事件
    RunFailedEvent,  # 运行失败事件
    ToolUseCompletedEvent,  # 工具使用完成事件
)
from app.core.models.execution_context import ExecutionContext  # 导入执行上下文模型
from app.core.models.run import Run, RunStatus  # 导入 Run 模型和状态枚举
from app.core.models.stored_message import StoredMessage  # 导入存储消息模型
from app.core.runtime.context_builder import ContextBuilder, NoTrimPolicy, SummaryPersistenceTarget  # 导入上下文构建器


@dataclass(frozen=True, slots=True)  # 使用不可变数据类，便于测试断言
class ChildAgentRunResult:
    """子代理执行结果。

    封装一次 child run 的核心标识和最终输出，供 TaskTool 构造
    工具结果返回给 master agent。

    Attributes:
        child_id: child 的会话内稳定标识符
        child_run_id: 本次 child run 的唯一标识符
        output: child agent 的最终文本输出
    """

    child_id: str  # child 会话内稳定标识符
    child_run_id: str  # 本次 child run 的唯一标识符
    output: str  # child agent 的最终文本输出


class ChildAgentRunner:
    """执行 child profile，并把历史隔离写入 child context。

    负责 child run 的完整生命周期管理，包括：
    1. 按 subagent_type 获取 child profile（大小写敏感匹配）
    2. 校验 resume 参数与 child 上下文的一致性
    3. 创建 child Run 并维护其终态
    4. 消费 child AgentLoop 事件并写入隔离的 child 上下文
    """

    # 通用子代理名称常量，匹配此名称时支持动态 tools 覆盖
    _GENERIC_AGENT_NAME = "Worker"

    def __init__(
        self,
        *,
        session_store,  # RedisSessionStore 实例，用于 child 上下文读写
        run_store,  # RedisRunStore 实例，用于 child run 状态管理
        redis,  # Redis 异步客户端实例
        agent_loop: AgentLoop,  # AgentLoop 编排器，用于驱动 child 执行
        child_profiles: dict[str, AgentExecutionProfile],  # 已注册的 child profile 映射表
        settings: Settings,  # 应用配置
        tool_catalog: dict[str, Tool] | None = None,  # 全局工具目录，用于动态构建子代理工具列表
        context_trim_policy=None,  # 可选的上下文裁剪策略，默认使用 NoTrimPolicy
    ) -> None:
        """初始化 ChildAgentRunner。

        Args:
            session_store: Redis 会话存储实例
            run_store: Redis Run 存储实例
            redis: Redis 异步客户端
            agent_loop: AgentLoop 编排器实例
            child_profiles: 按 subagent_type 索引的 child profile 字典
            settings: 应用配置对象
            tool_catalog: 全局工具目录，用于动态构建子代理工具列表
            context_trim_policy: 上下文裁剪策略，默认 NoTrimPolicy
        """
        self._session_store = session_store  # 保存会话存储引用
        self._run_store = run_store  # 保存运行存储引用
        self._redis = redis  # 保存 Redis 客户端引用
        self._agent_loop = agent_loop  # 保存 AgentLoop 引用
        self._child_profiles = child_profiles  # 保存 child profile 映射表
        self._settings = settings  # 保存应用配置
        self._tool_catalog = tool_catalog or {}  # 保存全局工具目录，用于动态构建工具列表
        self._context_trim_policy = context_trim_policy or NoTrimPolicy()  # 保存上下文裁剪策略，默认不裁剪
        self._child_locks: dict[tuple[str, str], asyncio.Lock] = {}  # 同一 session_id + child_id 的 child 执行必须串行

    async def run_child(
        self,
        *,
        session_id: str,  # 会话唯一标识
        parent_run_id: str,  # 父（master）run 的 ID
        tool_call_id: str,  # 触发本次 child 执行的 tool_call_id
        subagent_type: str,  # 子代理类型（大小写敏感，如 "Plan"）
        child_id: str,  # child 的会话内稳定标识符
        prompt: str,  # child 的初始任务 prompt
        description: str,  # child 的任务描述
        metadata: dict | None,  # 可选的请求元数据
        cancel_event: asyncio.Event | None,  # 可选的外部取消事件
        is_resume: bool = False,  # 是否为恢复已有 child 上下文
        tool_names: tuple[str, ...] | None = None,  # 可选的动态工具列表，仅对通用子代理生效
    ) -> ChildAgentRunResult:
        """执行一次 child run。

        完整流程：
        1. 获取 child profile（大小写敏感匹配）
        2. 校验 resume 参数（若 is_resume=True 且上下文不存在则报错）
        3. 创建 child Run（run_type=child, parent_run_id, child_id, tool_call_id）
        4. 构造 user message 并构建 child 上下文
        5. 消费 child loop 事件
        6. 更新 child run 终态
        7. 返回 ChildAgentRunResult

        Args:
            session_id: 会话唯一标识
            parent_run_id: 父 run ID
            tool_call_id: 触发工具调用 ID
            subagent_type: 子代理类型
            child_id: child 稳定标识
            prompt: 任务 prompt
            description: 任务描述，用于写入 child 摘要
            metadata: 请求元数据
            cancel_event: 外部取消事件
            is_resume: 是否为 resume 模式

        Returns:
            ChildAgentRunResult: 包含 child_id、child_run_id 和 output

        Raises:
            ValueError: 当 profile 不存在、resume 校验失败、或 child 执行失败时
        """
        profile = self._get_child_profile(subagent_type)  # 按 subagent_type 获取 profile（大小写敏感，未命中抛 UNKNOWN_SUBAGENT）
        # 通用子代理动态工具覆盖：当代理名称匹配且传入 tool_names 时，构建动态 profile
        if subagent_type == self._GENERIC_AGENT_NAME and tool_names is not None:
            profile = self._build_dynamic_profile(profile, tool_names)
        lock = self._child_locks.setdefault((session_id, child_id), asyncio.Lock())
        async with lock:  # 同一 child 上下文必须串行推进，保证 resume 语义线性一致
            await self._ensure_resume_matches_subagent(session_id, child_id, subagent_type, is_resume=is_resume)  # 校验 resume 一致性

            child_run_id = str(uuid.uuid4())  # 生成 child run 的唯一 ID
            created_at = datetime.now(timezone.utc)  # 获取当前 UTC 时间作为创建时间
            run = Run(  # 构造 child Run 实例
                run_id=child_run_id,  # 生成的唯一 ID
                session_id=session_id,  # 所属会话
                agent_id=profile.agent.agent_id,  # 使用 child profile 的 agent_id
                run_type="child",  # 标记为 child 类型
                parent_run_id=parent_run_id,  # 记录父 run ID
                child_id=child_id,  # 记录会话内稳定 child_id
                tool_call_id=tool_call_id,  # 记录触发工具调用 ID
                execution_mode="foreground",  # 前台执行模式
                status=RunStatus.RUNNING,  # 初始状态为运行中
                created_at=created_at,  # 创建时间
                updated_at=created_at,  # 初始更新时间与创建时间一致
                metadata=metadata,  # 透传请求元数据
            )
            pipeline = self._redis.pipeline()
            self._run_store.queue_create_run(
                pipeline,
                run,
                ttl_seconds=self._settings.run_ttl_seconds,
            )
            self._session_store.queue_add_session_run(
                pipeline,
                session_id=session_id,
                run_id=child_run_id,
                created_at_ts=created_at.timestamp(),
            )
            await pipeline.execute()

            try:
                user_message = StoredMessage.create(  # 构造 child 的初始用户消息
                    role="user",  # 用户角色
                    content=prompt,  # 任务 prompt
                    timestamp=datetime.now(timezone.utc),  # 当前 UTC 时间戳
                    child_id=child_id,  # 标记所属 child
                    subagent_type=subagent_type,  # 标记子代理类型，供 resume 校验使用
                )
                history, history_indices = await self._session_store.list_child_active_messages_with_indices(session_id, child_id)
                context_result = await ContextBuilder.build_llm_messages_with_repair_meta(
                    agent=profile.agent,
                    history=history,
                    history_indices=history_indices,
                    current_user_message=user_message,
                    trim_policy=self._context_trim_policy,
                    session_id=session_id,
                    summary_target=SummaryPersistenceTarget.for_child(session_id, child_id),
                    extra_system_messages=list(profile.extra_system_messages),
                )
                if context_result.history_dirty:
                    await self._session_store.mark_child_history_dirty(session_id, child_id)

                # 将首条 user message 落库与摘要写入合并为一次 pipeline，减少 Redis 往返
                pipeline = self._redis.pipeline()
                self._session_store.queue_append_child_message(
                    pipeline,
                    session_id=session_id,
                    child_id=child_id,
                    message=user_message,
                    source_run_id=child_run_id,
                    subagent_type=subagent_type,
                )
                self._session_store.queue_upsert_session_child_summary(
                    pipeline,
                    session_id=session_id,
                    child_id=child_id,
                    subagent_type=subagent_type,
                    description=description,
                )
                await pipeline.execute()

                output = await self._consume_child_loop(
                    session_id=session_id,
                    child_id=child_id,
                    child_run_id=child_run_id,
                    subagent_type=subagent_type,
                    profile=profile,
                    llm_messages=context_result.llm_messages,
                    metadata=metadata,
                    cancel_event=cancel_event,
                )
                finished_at = datetime.now(timezone.utc)
                await self._run_store.update_run_fields(
                    run_id=child_run_id,
                    status=RunStatus.COMPLETED,
                    finished_at=finished_at,
                    output=output,
                )
                return ChildAgentRunResult(child_id=child_id, child_run_id=child_run_id, output=output)
            except ValueError:
                raise
            except asyncio.CancelledError as error:
                finished_at = datetime.now(timezone.utc)
                await self._run_store.update_run_fields(
                    run_id=child_run_id,
                    status=RunStatus.CANCELLED,
                    finished_at=finished_at,
                    error_code=ErrorCode.RUN_CANCELLED,
                    error_message=str(error) if str(error) else "Run cancelled",
                )
                # 追加取消提示消息到 child 上下文，与主会话行为一致
                await self._session_store.append_child_message(
                    session_id, child_id,
                    StoredMessage.create(
                        role="system",
                        content="此次生成已被用户取消。",
                        timestamp=finished_at,
                        is_meta=True,
                        subagent_type=subagent_type,
                    ),
                    source_run_id=child_run_id,
                    subagent_type=subagent_type,
                )
                raise ValueError(f"{ErrorCode.CHILD_AGENT_EXECUTION_FAILED.value}: {str(error) if str(error) else 'Run cancelled'}")
            except Exception as error:
                finished_at = datetime.now(timezone.utc)
                await self._run_store.update_run_fields(
                    run_id=child_run_id,
                    status=RunStatus.FAILED,
                    finished_at=finished_at,
                    error_code=ErrorCode.CHILD_AGENT_EXECUTION_FAILED,
                    error_message=str(error),
                )
                raise ValueError(f"{ErrorCode.CHILD_AGENT_EXECUTION_FAILED.value}: {error}") from error

    def _get_child_profile(self, subagent_type: str) -> AgentExecutionProfile:
        """按 subagent_type 获取 child profile。

        使用大小写敏感的字典查找，"plan" 不会命中 "Plan"。
        若未找到匹配的 profile，抛出带有 UNKNOWN_SUBAGENT 错误码的 ValueError。

        Args:
            subagent_type: 子代理类型名称（大小写敏感）

        Returns:
            AgentExecutionProfile: 匹配的 child 执行配置

        Raises:
            ValueError: 当 subagent_type 未在 child_profiles 中注册时
        """
        try:  # 尝试直接按原始 key 查找
            return self._child_profiles[subagent_type]  # 大小写敏感的字典查找
        except KeyError as exc:  # 未找到时抛出带稳定错误码的异常
            raise ValueError(f"{ErrorCode.UNKNOWN_SUBAGENT.value}: {subagent_type}") from exc  # 包含子代理类型名便于调试

    def _build_dynamic_profile(
        self,
        base_profile: AgentExecutionProfile,
        tool_names: tuple[str, ...],
    ) -> AgentExecutionProfile:
        """基于基础 profile 构建动态 profile，使用指定的工具列表。

        Args:
            base_profile: 子代理的基础执行配置
            tool_names: 要注册的工具名称列表

        Returns:
            新的 AgentExecutionProfile 实例，包含动态构建的工具注册表
        """
        registry = ToolRegistry()  # 创建空的工具注册表
        for tool_name in tool_names:  # 遍历工具名称列表
            if tool_name in CHILD_FILTERED_TOOL_NAMES:  # 主控工具自动过滤，防止递归
                continue
            tool = self._tool_catalog.get(tool_name)  # 从全局工具目录查找
            if tool is None:  # 工具不存在时报错
                raise ValueError(
                    f"{ErrorCode.INVALID_SUBAGENT_CONFIG.value}: 未知工具: {tool_name}"
                )
            registry.register(tool)  # 注册工具

        return AgentExecutionProfile(
            agent_id=base_profile.agent_id,
            agent=base_profile.agent,
            prompt_source=base_profile.prompt_source,
            runtime=base_profile.runtime,
            tool_registry=registry,
            tool_hook_pipeline=base_profile.tool_hook_pipeline,
            max_turns=base_profile.max_turns,
            skills=base_profile.skills,
            extra_system_messages=base_profile.extra_system_messages,
        )

    async def _ensure_resume_matches_subagent(
        self, session_id: str, child_id: str, subagent_type: str, *, is_resume: bool = False
    ) -> None:
        """校验同一 child_id 不允许切换子代理类型。

        规则：
        1. 若 child 上下文不存在且 is_resume=True，抛出 CHILD_AGENT_CONTEXT_INVALID 错误
        2. 若 child 上下文不存在且 is_resume=False，允许（首次创建）
        3. 若 child 上下文已存在但 subagent_type 不匹配，抛出 CHILD_AGENT_CONTEXT_INVALID 错误
        4. 若 child 上下文已存在且 subagent_type 匹配，允许（续跑）

        Args:
            session_id: 会话唯一标识
            child_id: child 稳定标识符
            subagent_type: 请求的子代理类型
            is_resume: 是否为 resume 模式

        Raises:
            ValueError: 当 resume 校验不通过时
        """
        messages = await self._session_store.list_child_messages(session_id, child_id, start=0, end=0)  # 只读取首条消息，用于检查是否存在
        if not messages:  # child 上下文不存在（无任何历史消息）
            if is_resume:  # resume 模式下，不存在的 child 上下文是非法操作
                raise ValueError(f"{ErrorCode.CHILD_AGENT_CONTEXT_INVALID.value}: child 上下文不存在: {child_id}")
            return  # 首次创建，校验通过
        existing_type = messages[0].meta.subagent_type  # 读取已有消息中记录的 subagent_type
        if existing_type is not None and existing_type != subagent_type:  # 已有类型与请求类型不匹配
            raise ValueError(f"{ErrorCode.CHILD_AGENT_CONTEXT_INVALID.value}: {child_id}")

    async def _consume_child_loop(
        self,
        *,
        session_id: str,  # 会话唯一标识
        child_id: str,  # child 稳定标识符
        child_run_id: str,  # child run ID
        subagent_type: str,  # 子代理类型
        profile: AgentExecutionProfile,  # child 执行配置
        llm_messages: list[dict],  # 已构建的 LLM 消息列表
        metadata: dict | None,  # 请求元数据
        cancel_event: asyncio.Event | None,  # 外部取消事件
    ) -> str:
        """消费 child AgentLoop 事件，并写入 child 隔离上下文。

        事件处理策略：
        - MessageDeltaEvent: 收集文本片段用于最终输出
        - AssistantWithToolsEvent: 写入 assistant 消息到 child 上下文
        - ToolUseCompletedEvent: 写入 tool 结果消息到 child 上下文
        - RunCompletedEvent: 写入最终 assistant 成稿到 child 上下文，返回 output
        - RunFailedEvent: 更新 child run 为 failed 状态，抛出异常
        - RunCancelledEvent: 更新 child run 为 cancelled 状态，抛出异常

        Args:
            session_id: 会话唯一标识
            child_id: child 稳定标识符
            child_run_id: child run ID
            subagent_type: 子代理类型
            profile: child 执行配置
            llm_messages: 已构建的 LLM 消息列表
            metadata: 请求元数据
            cancel_event: 外部取消事件

        Returns:
            str: child agent 的最终文本输出

        Raises:
            ValueError: 当 child 执行失败或被取消时
        """
        text_parts: list[str] = []  # 初始化文本片段收集器
        execution_context = ExecutionContext(  # 构造 child 执行上下文
            run_id=child_run_id,  # child run ID
            session_id=session_id,  # 所属会话
            metadata=metadata,  # 请求元数据
            agent=profile.agent,  # child Agent 配置
            cancel_event=cancel_event or asyncio.Event(),  # 外部取消事件或新的默认事件
            run_type="child",  # 标记为 child 类型
            child_id=child_id,  # 传入 child 标识符，用于 plan/task 隔离
        )
        async for event in self._agent_loop.run(  # 驱动 AgentLoop 执行
            run_id=child_run_id,  # child run ID
            profile=profile,  # child 执行配置
            messages=llm_messages,  # 初始 LLM 消息列表
            session_id=session_id,  # 所属会话
            context=execution_context,  # child 执行上下文
        ):
            if isinstance(event, MessageDeltaEvent):  # 流式文本增量
                text_parts.append(event.content)  # 累积文本片段
            elif isinstance(event, AssistantWithToolsEvent):  # assistant 工具请求
                await self._session_store.append_child_message(  # 写入 child 上下文
                    session_id,  # 所属会话
                    child_id,  # 所属 child
                    StoredMessage.create(  # 构造 assistant 消息
                        role="assistant",  # assistant 角色
                        content=event.content,  # 文本内容
                        tool_calls=event.tool_calls,  # 工具调用列表
                        reasoning_content=event.reasoning_content,  # 思考内容
                        timestamp=datetime.now(timezone.utc),  # 当前时间戳
                        subagent_type=subagent_type,  # 子代理类型
                    ),
                    source_run_id=child_run_id,  # 来源 run ID
                    subagent_type=subagent_type,  # 子代理类型
                )
            elif isinstance(event, ToolUseCompletedEvent):  # 工具执行完成
                await self._session_store.append_child_message(  # 写入 child 上下文
                    session_id,  # 所属会话
                    child_id,  # 所属 child
                    StoredMessage.create(  # 构造 tool 结果消息
                        role="tool",  # tool 角色
                        content=event.result,  # 工具结果
                        tool_call_id=event.tool_call_id,  # 工具调用 ID
                        name=event.tool_name,  # 工具名称
                        timestamp=datetime.now(timezone.utc),  # 当前时间戳
                        subagent_type=subagent_type,  # 子代理类型
                    ),
                    source_run_id=child_run_id,  # 来源 run ID
                    subagent_type=subagent_type,  # 子代理类型
                )
            elif isinstance(event, RunCompletedEvent):  # child 正常完成
                output = event.output  # 获取最终输出
                await self._session_store.append_child_message(  # 写入最终 assistant 消息到 child 上下文
                    session_id,  # 所属会话
                    child_id,  # 所属 child
                    StoredMessage.create(  # 构造 assistant 消息
                        role="assistant",  # assistant 角色
                        content=output or None,  # 最终输出内容
                        reasoning_content=event.reasoning_content,  # 思考内容
                        timestamp=datetime.now(timezone.utc),  # 当前时间戳
                        subagent_type=subagent_type,  # 子代理类型
                    ),
                    source_run_id=child_run_id,  # 来源 run ID
                    subagent_type=subagent_type,  # 子代理类型
                )
                return output  # 返回最终输出，结束循环
            elif isinstance(event, RunFailedEvent):  # child 执行失败
                finished_at = datetime.now(timezone.utc)  # 获取失败时间
                await self._run_store.update_run_fields(  # 更新 child run 为失败状态
                    run_id=child_run_id,  # child run ID
                    status=RunStatus.FAILED,  # 失败状态
                    finished_at=finished_at,  # 失败时间
                    error_code=event.error_code,  # 错误码
                    error_message=event.message,  # 错误消息
                )
                raise ValueError(f"{ErrorCode.CHILD_AGENT_EXECUTION_FAILED.value}: {event.message}")  # 向上抛出异常
            elif isinstance(event, RunCancelledEvent):  # child 被取消
                finished_at = datetime.now(timezone.utc)  # 获取取消时间
                await self._run_store.update_run_fields(  # 更新 child run 为取消状态
                    run_id=child_run_id,  # child run ID
                    status=RunStatus.CANCELLED,  # 取消状态
                    finished_at=finished_at,  # 取消时间
                    error_code=event.error_code,  # 错误码
                    error_message=event.reason,  # 取消原因
                )
                # 追加取消提示消息到 child 上下文，与主会话行为一致
                await self._session_store.append_child_message(
                    session_id, child_id,
                    StoredMessage.create(
                        role="system",
                        content="此次生成已被用户取消。",
                        timestamp=finished_at,
                        is_meta=True,
                        subagent_type=subagent_type,
                    ),
                    source_run_id=child_run_id,
                    subagent_type=subagent_type,
                )
                raise ValueError(f"{ErrorCode.CHILD_AGENT_EXECUTION_FAILED.value}: {event.reason}")  # 向上抛出异常
        return "".join(text_parts)  # 如果循环结束后未返回，拼接所有文本作为输出兜底
