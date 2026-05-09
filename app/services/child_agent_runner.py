"""子代理执行服务。

提供 ChildAgentRunner，负责执行 child profile 并把执行过程中的
历史消息隔离写入 child context，维护 child run 的完整生命周期。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import uuid

from app.config import Settings
from app.core.loop.agent_loop import AgentLoop
from app.core.models.agent import Agent, AgentExecutionProfile
from app.core.models.error import ErrorCode
from app.core.models.tool import Tool, ToolRegistry
from app.infra.agents.profile_builder import CHILD_FILTERED_TOOL_NAMES
from app.core.models.event import (
    AssistantWithToolsEvent,
    MessageDeltaEvent,
    RunCancelledEvent,
    RunCompletedEvent,
    RunFailedEvent,
    ToolUseCompletedEvent,
)
from app.core.models.execution_context import ExecutionContext
from app.core.models.run import Run, RunStatus
from app.core.models.stored_message import StoredMessage
from app.core.runtime.context_builder import ContextBuilder, NoTrimPolicy, SummaryPersistenceTarget


@dataclass(frozen=True, slots=True)
class ChildAgentRunResult:
    """子代理执行结果。"""

    child_id: str
    child_run_id: str
    output: str


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
        session_store,
        run_store,
        redis,
        agent_loop: AgentLoop,
        child_profiles: dict[str, AgentExecutionProfile],
        settings: Settings,
        tool_catalog: dict[str, Tool] | None = None,
        context_trim_policy=None,
    ) -> None:
        self._session_store = session_store
        self._run_store = run_store
        self._redis = redis
        self._agent_loop = agent_loop
        self._child_profiles = child_profiles
        self._settings = settings
        self._tool_catalog = tool_catalog or {}
        self._context_trim_policy = context_trim_policy or NoTrimPolicy()
        self._child_locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def run_child(
        self,
        *,
        session_id: str,
        parent_run_id: str,
        tool_call_id: str,
        subagent_type: str,
        child_id: str,
        prompt: str,
        description: str,
        metadata: dict | None,
        cancel_event: asyncio.Event | None,
        is_resume: bool = False,
        tool_names: tuple[str, ...] | None = None,
    ) -> ChildAgentRunResult:
        """执行一次 child run。"""
        profile = self._get_child_profile(subagent_type)
        # 通用子代理动态工具覆盖：当代理名称匹配且传入 tool_names 时，构建动态 profile
        if subagent_type == self._GENERIC_AGENT_NAME and tool_names is not None:
            profile = self._build_dynamic_profile(profile, tool_names)
        lock = self._child_locks.setdefault((session_id, child_id), asyncio.Lock())
        async with lock:
            await self._ensure_resume_matches_subagent(session_id, child_id, subagent_type, is_resume=is_resume)

            child_run_id = str(uuid.uuid4())
            created_at = datetime.now(timezone.utc)
            run = Run(
                run_id=child_run_id,
                session_id=session_id,
                agent_id=profile.agent.agent_id,
                run_type="child",
                parent_run_id=parent_run_id,
                child_id=child_id,
                tool_call_id=tool_call_id,
                execution_mode="foreground",
                status=RunStatus.RUNNING,
                created_at=created_at,
                updated_at=created_at,
                metadata=metadata,
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
                user_message = StoredMessage.create(
                    role="user",
                    content=prompt,
                    timestamp=datetime.now(timezone.utc),
                    child_id=child_id,
                    subagent_type=subagent_type,
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
        """按 subagent_type 获取 child profile。"""
        try:
            return self._child_profiles[subagent_type]
        except KeyError as exc:
            raise ValueError(f"{ErrorCode.UNKNOWN_SUBAGENT.value}: {subagent_type}") from exc

    def _build_dynamic_profile(
        self,
        base_profile: AgentExecutionProfile,
        tool_names: tuple[str, ...],
    ) -> AgentExecutionProfile:
        """基于基础 profile 构建动态 profile，使用指定的工具列表。"""
        registry = ToolRegistry()
        for tool_name in tool_names:
            if tool_name in CHILD_FILTERED_TOOL_NAMES:
                continue
            tool = self._tool_catalog.get(tool_name)
            if tool is None:
                raise ValueError(
                    f"{ErrorCode.INVALID_SUBAGENT_CONFIG.value}: 未知工具: {tool_name}"
                )
            registry.register(tool)

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
        """校验同一 child_id 不允许切换子代理类型。"""
        messages = await self._session_store.list_child_messages(session_id, child_id, start=0, end=0)
        if not messages:
            if is_resume:
                raise ValueError(f"{ErrorCode.CHILD_AGENT_CONTEXT_INVALID.value}: child 上下文不存在: {child_id}")
            return
        existing_type = messages[0].meta.subagent_type
        if existing_type is not None and existing_type != subagent_type:
            raise ValueError(f"{ErrorCode.CHILD_AGENT_CONTEXT_INVALID.value}: {child_id}")

    async def _consume_child_loop(
        self,
        *,
        session_id: str,
        child_id: str,
        child_run_id: str,
        subagent_type: str,
        profile: AgentExecutionProfile,
        llm_messages: list[dict],
        metadata: dict | None,
        cancel_event: asyncio.Event | None,
    ) -> str:
        """消费 child AgentLoop 事件，并写入 child 隔离上下文。"""
        text_parts: list[str] = []
        execution_context = ExecutionContext(
            run_id=child_run_id,
            session_id=session_id,
            metadata=metadata,
            agent=profile.agent,
            cancel_event=cancel_event or asyncio.Event(),
            run_type="child",
            child_id=child_id,
        )
        async for event in self._agent_loop.run(
            run_id=child_run_id,
            profile=profile,
            messages=llm_messages,
            session_id=session_id,
            context=execution_context,
        ):
            if isinstance(event, MessageDeltaEvent):
                text_parts.append(event.content)
            elif isinstance(event, AssistantWithToolsEvent):
                await self._session_store.append_child_message(
                    session_id, child_id,
                    StoredMessage.create(
                        role="assistant",
                        content=event.content,
                        tool_calls=event.tool_calls,
                        reasoning_content=event.reasoning_content,
                        timestamp=datetime.now(timezone.utc),
                        subagent_type=subagent_type,
                    ),
                    source_run_id=child_run_id,
                    subagent_type=subagent_type,
                )
            elif isinstance(event, ToolUseCompletedEvent):
                await self._session_store.append_child_message(
                    session_id, child_id,
                    StoredMessage.create(
                        role="tool",
                        content=event.result,
                        tool_call_id=event.tool_call_id,
                        name=event.tool_name,
                        timestamp=datetime.now(timezone.utc),
                        subagent_type=subagent_type,
                    ),
                    source_run_id=child_run_id,
                    subagent_type=subagent_type,
                )
            elif isinstance(event, RunCompletedEvent):
                output = event.output
                await self._session_store.append_child_message(
                    session_id, child_id,
                    StoredMessage.create(
                        role="assistant",
                        content=output or None,
                        reasoning_content=event.reasoning_content,
                        timestamp=datetime.now(timezone.utc),
                        subagent_type=subagent_type,
                    ),
                    source_run_id=child_run_id,
                    subagent_type=subagent_type,
                )
                return output
            elif isinstance(event, RunFailedEvent):
                finished_at = datetime.now(timezone.utc)
                await self._run_store.update_run_fields(
                    run_id=child_run_id,
                    status=RunStatus.FAILED,
                    finished_at=finished_at,
                    error_code=event.error_code,
                    error_message=event.message,
                )
                raise ValueError(f"{ErrorCode.CHILD_AGENT_EXECUTION_FAILED.value}: {event.message}")
            elif isinstance(event, RunCancelledEvent):
                finished_at = datetime.now(timezone.utc)
                await self._run_store.update_run_fields(
                    run_id=child_run_id,
                    status=RunStatus.CANCELLED,
                    finished_at=finished_at,
                    error_code=event.error_code,
                    error_message=event.reason,
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
                raise ValueError(f"{ErrorCode.CHILD_AGENT_EXECUTION_FAILED.value}: {event.reason}")
        return "".join(text_parts)
