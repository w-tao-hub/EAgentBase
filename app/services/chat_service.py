"""ChatService 实现。

提供聊天主链路编排，并将锁管理与事件处理委托给独立协作者。
"""

from __future__ import annotations  # 启用未来注解

import asyncio  # 导入异步模块，用于取消事件与后台任务
from datetime import datetime, timezone  # 导入日期时间类和 UTC 时区
from dataclasses import dataclass  # 导入数据类，用于在流式处理时保存终态结果
import logging  # 导入标准库日志模块，避免 services 依赖 infra 包路径
from typing import TYPE_CHECKING, AsyncIterator  # 导入类型检查标记和异步迭代器
import uuid  # 导入 UUID 生成模块

from app.core.models.execution_context import ExecutionContext  # 导入执行上下文模型
from app.core.models.event import Event, RequestFailedEvent, RunCompletedEvent, RunFailedEvent, RunCancelledEvent  # 导入服务层直接消费的事件模型
from app.core.models.error import ErrorCode  # 错误码枚举
from app.core.models.stored_message import StoredMessage  # 消息模型
from app.core.models.run import Run, RunStatus  # Run 模型和状态枚举
from app.core.runtime.context_builder import (  # 导入上下文构建器、默认策略与压缩异常
    ContextBuilder,
    ContextCompressionError,
    NoTrimPolicy,
    SummaryPersistenceTarget,
)
from app.services.chat_event_processor import ChatEventProcessor, PendingSessionWriteBuffer  # 导入聊天事件分发器
from app.services.chat_run_lock import (  # 导入锁作用域协作者
    ChatRunLockHeartbeatLostError,
    ChatRunLockNotAcquiredError,
    ChatRunLockScope,
)

# 获取模块级日志器。
# 直接使用标准库 logging，保持 services 层不依赖 infra 包路径。
logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # 仅在类型检查时导入
    from app.core.models.agent import Agent, AgentExecutionProfile  # Agent 领域模型和执行配置
    from app.core.runtime.context_builder import ContextTrimPolicy  # 上下文策略协议
    from app.infra.store.redis_session_store import RedisSessionStore  # 会话存储类型
    from app.infra.store.redis_run_store import RedisRunStore  # Run 存储类型
    from app.infra.store.redis_lock_store import RedisLockStore  # 锁存储类型
    from app.services.agent_provider import AgentProvider  # Agent 提供者协议
    from app.core.loop.agent_loop import AgentLoop  # Agent 循环类型
    from app.config import Settings  # 应用配置类型


@dataclass(slots=True)
class ConsumedLoopState:
    """AgentLoop 事件消费状态。

    因为 ChatService 既要一边向外流式 yield 事件，
    又要在循环结束后拿到终态结果，所以使用显式状态载体保存终态信息。
    """

    terminal_event: RunCompletedEvent | RunFailedEvent | RunCancelledEvent | None = None  # 保存终态事件
    final_output: str = ""  # 保存完成态的完整输出


class ChatService:
    """聊天服务。

    负责聊天主链路的完整编排，包括：
    1. 会话存在性校验
    2. 会话锁获取（防止并发 Run）
    3. 用户消息持久化
    4. 上下文构建与 Runtime 调用
    5. 事件流转发（委托给 ChatEventProcessor）
    6. 终态持久化（Run 状态 + 助手消息）
    7. 锁释放（委托给 ChatRunLockScope）
    8. 终态事件发出

    重要约束：
    - 终态事件必须在"Run 终态持久化 + 助手成稿写回 + 锁释放"之后发出
    - run_failed 时不写入助手消息
    """

    def __init__(  # 构造函数
        self,
        session_store: RedisSessionStore,  # 会话存储实例
        run_store: RedisRunStore,  # Run 存储实例
        lock_store: RedisLockStore,  # 锁存储实例
        agent_provider: AgentProvider,  # Agent 提供者实例
        agent_loop: AgentLoop,  # Agent 循环实例
        settings: Settings,  # 应用配置实例
        redis: object,  # Redis 异步客户端，用于跨进程取消广播
        pubsub_redis: object | None = None,  # pubsub 专用 Redis 客户端，未提供时退化复用主 Redis
        context_trim_policy: ContextTrimPolicy | None = None,  # 上下文策略（默认不裁剪）
        event_processor: ChatEventProcessor | None = None,  # 聊天事件分发器（默认按 session_store 创建）
    ) -> None:
        """初始化 ChatService。

        extra_system_messages 不再由构造注入，改为运行时从 master profile 读取。

        Args:
            session_store: 用于会话元数据和消息历史的存储
            run_store: 用于 Run 状态的存储
            lock_store: 用于会话锁的获取和释放
            agent_provider: 用于获取当前 Agent 执行配置（profile）
            agent_loop: 用于执行单次 Run 的 Agent 循环
            settings: 应用配置，用于读取 session_lock_ttl_seconds 等配置项
            redis: 主 Redis 异步客户端，用于普通命令与取消广播 publish
            pubsub_redis: pubsub 专用 Redis 客户端；未提供时复用主 Redis，便于测试替身复用
            context_trim_policy: 上下文裁剪/压缩策略；未提供时默认使用 NoTrimPolicy
            event_processor: 聊天事件分发器；未提供时默认基于 session_store 创建
        """
        self._session_store = session_store  # 保存会话存储引用
        self._run_store = run_store  # 保存 Run 存储引用
        self._lock_store = lock_store  # 保存锁存储引用
        self._agent_provider = agent_provider  # 保存 Agent 提供者引用
        self._agent_loop = agent_loop  # 保存 Agent 循环引用
        self._settings = settings  # 保存配置引用
        self._redis = redis  # 保存主 Redis 客户端引用，负责 publish 与普通命令
        self._pubsub_redis = pubsub_redis or redis  # 保存 pubsub 专用 Redis 客户端，默认回退复用主 Redis
        self._context_trim_policy = context_trim_policy or NoTrimPolicy()  # 保存上下文策略，默认不裁剪
        self._event_processor = event_processor or ChatEventProcessor(session_store)  # 保存事件分发器，默认使用会话存储创建
        self._active_cancel_events: dict[str, asyncio.Event] = {}  # 本地活跃 run 的取消事件表，用于进程内快速取消
        self._cancel_channel_pattern = "run_cancel:*"  # 全局模式订阅使用的频道模式，覆盖所有 run 的取消广播
        self._cancel_channel_prefix = "run_cancel:"  # 频道名前缀，用于从收到的消息里解析 run_id
        self._cancel_listener_task: asyncio.Task[None] | None = None  # 全局取消监听后台任务，整个进程只保留一条
        self._cancel_pubsub: object | None = None  # 全局共享的 pubsub 实例，对应专用 Redis 长连接
        self._cancel_listener_lock = asyncio.Lock()  # 保护监听器启动与关闭的协程锁，避免并发重复创建
        self._cancel_listener_closed = False  # 记录监听器是否已经显式关闭，避免 shutdown 后被误重启

    async def start_cancel_listener(self) -> bool:
        """启动全局 run_cancel 监听器。

        Returns:
            bool: 监听器是否已经处于可用状态
        """
        if self._cancel_listener_closed:  # shutdown 后不再允许重启监听器，避免资源反复创建
            logger.warning("取消监听器已关闭，忽略启动请求")
            return False

        if self._cancel_listener_task is not None and not self._cancel_listener_task.done():  # 已有活跃监听任务时直接复用
            return True

        async with self._cancel_listener_lock:  # 启动路径串行化，避免并发请求下重复创建 pubsub 长连接
            if self._cancel_listener_closed:  # 二次检查，防止等待锁期间容器已经进入关闭流程
                logger.warning("取消监听器已关闭，忽略启动请求")
                return False

            if self._cancel_listener_task is not None and not self._cancel_listener_task.done():  # 双重检查现有监听任务是否已可用
                return True

            stale_pubsub = self._cancel_pubsub  # 保留旧 pubsub，便于在重新启动前先完成清理
            self._cancel_pubsub = None  # 先清空引用，避免关闭过程中被其他协程误判为仍可用
            if stale_pubsub is not None:  # 若前一次监听异常退出但 pubsub 仍残留，则先把旧连接释放掉
                await self._close_cancel_pubsub(stale_pubsub)

            try:
                pubsub = self._pubsub_redis.pubsub()  # 从专用 Redis 客户端创建共享 pubsub 长连接
                await pubsub.psubscribe(self._cancel_channel_pattern)  # 一次性订阅所有 run_cancel:* 模式频道
            except Exception as error:  # noqa: BLE001 - 监听器属于增强能力，启动失败不应中断聊天主链路
                logger.error("启动全局取消监听器失败: error=%s", error, exc_info=True)
                return False

            self._cancel_pubsub = pubsub  # 保存共享 pubsub 引用，供关闭阶段统一退订与回收
            self._cancel_listener_task = asyncio.create_task(
                self._listen_cancel_messages(pubsub),
                name="chat-service-run-cancel-listener",
            )  # 启动全局后台监听任务，后续所有 SSE run 共享这一条连接
            logger.info("全局取消监听器已启动: pattern=%s", self._cancel_channel_pattern)
            return True

    async def aclose(self) -> None:
        """关闭 ChatService 自身持有的后台资源。"""
        async with self._cancel_listener_lock:  # 关闭路径同样串行化，避免和启动流程交错
            self._cancel_listener_closed = True  # 标记进入关闭态，阻止后续请求重新拉起监听器
            listener_task = self._cancel_listener_task  # 取出当前监听任务，准备在锁外取消并等待回收
            pubsub = self._cancel_pubsub  # 取出共享 pubsub，准备在锁外执行退订和关闭
            self._cancel_listener_task = None  # 先清空任务引用，避免关闭过程中被误复用
            self._cancel_pubsub = None  # 先清空 pubsub 引用，保持对象状态与实际生命周期一致

        if listener_task is not None:  # 若后台监听任务存在，则先发出取消信号并等待结束
            listener_task.cancel()
            try:
                await listener_task
            except asyncio.CancelledError:  # 任务被正常取消属于预期路径，不再额外记录错误
                pass

        if pubsub is not None:  # 最后关闭共享 pubsub 长连接，释放专用连接池占用的唯一连接
            await self._close_cancel_pubsub(pubsub)

    async def _close_cancel_pubsub(self, pubsub: object) -> None:
        """关闭共享取消监听 pubsub。"""
        try:
            await pubsub.punsubscribe(self._cancel_channel_pattern)  # 显式退订模式频道，减少服务端残留状态
        except Exception as error:  # noqa: BLE001 - 关闭阶段应尽量继续释放其余资源
            logger.warning("取消监听器退订失败: error=%s", error, exc_info=True)

        try:
            await pubsub.aclose()  # 关闭 pubsub 对象本身，释放底层长连接
        except Exception as error:  # noqa: BLE001 - 关闭阶段应尽量继续释放其余资源
            logger.warning("取消监听器关闭 pubsub 失败: error=%s", error, exc_info=True)

    async def stream_chat(  # 流式聊天
        self,
        session_id: str,  # 会话唯一标识
        user_message: str,  # 用户输入消息
        metadata: dict | None = None,  # 请求元数据
        cancel_event: asyncio.Event | None = None,  # 外部注入的取消事件，用于 SSE 断开或主动取消
    ) -> AsyncIterator[Event]:  # 返回事件流
        """执行流式聊天。

        按照以下步骤执行：
        1. 检查会话是否存在
        2. 生成 run_id
        3. 尝试获取会话锁
        4. 如果锁获取失败，返回 request_failed 事件
        5. 构造当前用户消息
        6. 获取 Agent 配置
        7. 获取会话历史消息（仅旧历史）
        8. 构建上下文（system + history + current user message）
        9. 持久化用户消息
        10. 创建 Run 记录（status=RUNNING，包含 metadata）
        11. 调用 AgentLoop.run()，传递 metadata
        12. 转发 run_started 事件
        13. 转发 message_delta 事件
        14. 判断终态类型（run_completed 或 run_failed）
        15. 持久化终态（Run 状态 + 助手消息，metadata 会保留）
        16. 释放会话锁
        17. 发出终态事件

        Args:
            session_id: 会话唯一标识
            user_message: 用户输入消息
            metadata: 请求元数据（可选），包含权限信息、业务上下文等

        Yields:
            Event: 业务事件流
        """
        logger.info("开始流式聊天: session_id=%s", session_id)

        # Step 1: 检查会话是否存在。
        # 若会话不存在，直接返回 request_failed，后续无需进入任何锁或运行逻辑。
        session = await self._session_store.get_session(session_id)
        if session is None:
            logger.error("会话不存在: session_id=%s", session_id)
            yield RequestFailedEvent(
                error_code=ErrorCode.SESSION_NOT_FOUND,
                message=f"Session {session_id} not found",
            )
            return

        # Step 2: 提前生成 run_id，便于锁 owner、Run 持久化与后续日志统一使用。
        run_id = str(uuid.uuid4())
        logger.info("生成 Run ID: run_id=%s", run_id)
        terminal_event: RunCompletedEvent | RunFailedEvent | RunCancelledEvent | None = None  # 提前初始化终态事件，便于异常分支复用
        final_output = ""  # 提前初始化最终输出，便于异常分支沿用现有持久化接口
        run_created = False  # 记录当前 run 是否已经建档，便于失锁后决定是否需要落失败态

        # 独立 pubsub 监听器属于增强能力。
        # 这里做一次幂等确保：正常服务启动时会由容器预热，测试或特殊装配路径则由首次请求兜底拉起。
        await self.start_cancel_listener()

        try:
            # Step 3: 进入聊天运行锁作用域。
            # 锁在退出作用域时统一释放，避免 acquire/release 散落在方法体内。
            async with self._create_run_lock_scope(session_id=session_id, run_id=run_id):
                effective_cancel_event = cancel_event if cancel_event is not None else asyncio.Event()
                self._active_cancel_events[run_id] = effective_cancel_event
                pending_write_buffer = self._event_processor.create_pending_write_buffer(run_id=run_id)  # 提前创建后台写链，确保 finally/取消补偿路径也能安全 flush
                logger.info(  # 记录活跃 run 进入本地取消表，便于从 app.log 观察字典增减
                    "登记本地取消事件: run_id=%s, active_cancel_events=%d",
                    run_id,
                    len(self._active_cancel_events),
                )
                try:
                    # Step 4: 准备当前请求上下文，包括用户消息、Agent、历史、LLM 消息和 Run。
                    user_message_model = self._build_user_message(user_message)
                    # 从 provider 获取 master profile，替代旧的 get_default() 单一 Agent 方式
                    profile = self._agent_provider.get_default_profile()  # 获取主 Agent 执行配置
                    agent = profile.agent  # 从 profile 解构 Agent 静态配置
                    history, history_indices = await self._session_store.list_active_main_messages_with_indices(session_id)
                    context_build_result = await ContextBuilder.build_llm_messages_with_repair_meta(
                        agent=agent,
                        history=history,
                        history_indices=history_indices,
                        current_user_message=user_message_model,
                        trim_policy=self._context_trim_policy,
                        session_id=session_id,
                        summary_target=SummaryPersistenceTarget.for_main(session_id),
                        extra_system_messages=list(profile.extra_system_messages),  # 从 profile 读取运行时 system 提示
                    )
                    llm_messages = context_build_result.llm_messages  # 取出最终要发给模型的消息列表。
                    if context_build_result.history_dirty:  # 若检测到历史工具配对错乱，则仅打脏标记，不阻塞当前请求。
                        await self._session_store.mark_main_history_dirty(session_id)

                    # Step 5: 仍然保持“先持久化用户消息，再调用 AgentLoop”的语义。
                    await self._session_store.append_main_message(
                        session_id=session_id,
                        message=user_message_model,
                        source_run_id=run_id,
                    )

                    # Step 6: 创建并持久化 RUNNING 状态的 Run 记录。
                    run = self._build_running_run(
                        run_id=run_id,
                        session_id=session_id,
                        agent=agent,
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
                        run_id=run_id,
                        created_at_ts=run.created_at.timestamp(),
                    )
                    await pipeline.execute()
                    run_created = True  # 标记 Run 已建档，后续若失锁需要把它收敛为失败态

                    # Step 7: 构造执行上下文，供 AgentLoop / Runtime / Hook 使用。
                    execution_context = ExecutionContext(
                        run_id=run_id,
                        session_id=session_id,
                        metadata=metadata,
                        agent=agent,  # Agent 静态配置，从 profile.agent 解构
                        cancel_event=effective_cancel_event,
                        run_type="master",  # 主运行模式，区别于子代理的 child 运行
                    )

                    # Step 8: 处理 AgentLoop 事件流。
                    # 事件的转发与内部落库由 ChatEventProcessor 负责，ChatService 只保留编排骨架。
                    loop_state = ConsumedLoopState()
                    try:
                        async for outbound_event in self._consume_loop_events(
                            session_id=session_id,
                            run_id=run_id,
                            profile=profile,  # 传递 master profile，替代旧 agent 参数
                            llm_messages=llm_messages,
                            execution_context=execution_context,
                            loop_state=loop_state,
                            pending_write_buffer=pending_write_buffer,
                        ):
                            yield outbound_event
                    except Exception as error:
                        # 后台写链 flush 失败也会从这里冒泡。
                        # 这类异常必须在锁内收敛为失败态，不能等锁释放后再补写。
                        if run_created:
                            terminal_event = self._build_unexpected_run_failed_event(run_id=run_id, error=error)
                            final_output = ""
                            await self._persist_terminal_state(
                                session_id=session_id,
                                run_id=run_id,
                                terminal_event=terminal_event,
                                final_output=final_output,
                            )
                        raise

                    terminal_event = loop_state.terminal_event  # 保存终态事件，供锁释放后再对外发出
                    final_output = loop_state.final_output  # 保存终态输出，用于完成态落库与最终日志

                    # Step 9: 在锁作用域内完成终态落库，保持“先落库，再释放锁，再发终态事件”。
                    await self._persist_terminal_state(
                        session_id=session_id,
                        run_id=run_id,
                        terminal_event=terminal_event,
                        final_output=final_output,
                    )
                finally:
                    removed_event = self._active_cancel_events.pop(run_id, None)  # 当前 run 结束后立即移除本地取消映射，避免长驻内存
                    if removed_event is not None:  # 只有确实移除了本地事件才记录日志，避免重复关闭时误导观测
                        logger.info(
                            "移除本地取消事件: run_id=%s, active_cancel_events=%d",
                            run_id,
                            len(self._active_cancel_events),
                        )
                    # 若因客户端断开（GeneratorExit）导致流被提前关闭，
                    # 外部 monitor 在 finally 中设置 cancel_event 可能存在时间差。
                    # 这里先短暂等待，给外部一个设置 cancel_event 的机会。
                    if terminal_event is None and not effective_cancel_event.is_set():
                        try:
                            await asyncio.wait_for(effective_cancel_event.wait(), timeout=0.5)
                        except asyncio.TimeoutError:
                            pass
                    # 如果 cancel_event 已被设置，则补持久化 CANCELLED 状态（finally 中不能 yield）。
                    if terminal_event is None and effective_cancel_event.is_set():
                        await pending_write_buffer.flush()  # 提前刷完中间后台写，继续保持“先落库，再释放锁，再发终态”的约束
                        terminal_event = RunCancelledEvent(
                            run_id=run_id,
                            reason="Run cancelled by client disconnect",
                            error_code=ErrorCode.RUN_CANCELLED,
                        )
                        await self._persist_terminal_state(
                            session_id=session_id,
                            run_id=run_id,
                            terminal_event=terminal_event,
                            final_output="",
                        )

            # Step 10: 锁作用域退出后，再发出终态事件，确保对外观察到的顺序不变。
            if terminal_event is not None:
                if isinstance(terminal_event, RunCompletedEvent):
                    logger.info("Run 完成: run_id=%s, output_length=%d", run_id, len(final_output))
                elif isinstance(terminal_event, RunFailedEvent):
                    logger.error("Run 失败: run_id=%s, error=%s", run_id, terminal_event.message)
                elif isinstance(terminal_event, RunCancelledEvent):
                    logger.warning("Run 被取消: run_id=%s, reason=%s", run_id, terminal_event.reason)
                yield terminal_event

        except ChatRunLockHeartbeatLostError as error:
            logger.error("聊天运行锁心跳丢失: session_id=%s, run_id=%s, error=%s", session_id, run_id, error)
            terminal_event = RunFailedEvent(
                run_id=run_id,
                error_code=ErrorCode.SESSION_LOCK_HEARTBEAT_FAILED,
                message=str(error),
            )
            if run_created:  # 只有已经建档的 run 才需要同步收敛为失败态
                await self._persist_terminal_state(
                    session_id=session_id,
                    run_id=run_id,
                    terminal_event=terminal_event,
                    final_output="",
                )
            yield terminal_event
        except ChatRunLockNotAcquiredError:
            # 锁获取失败时，保持旧行为：返回 request_failed，并带上预先生成的 run_id。
            yield RequestFailedEvent(
                error_code=ErrorCode.SESSION_RUN_CONFLICT,
                message=f"Session {session_id} already has an active run",
                run_id=run_id,
            )
        except ContextCompressionError as error:
            logger.error("上下文压缩失败: session_id=%s, run_id=%s, error=%s", session_id, run_id, error)
            if run_created:
                yield RunFailedEvent(
                    run_id=run_id,
                    error_code=ErrorCode.CONTEXT_COMPRESSION_FAILED,
                    message=str(error),
                )
            else:
                yield RequestFailedEvent(
                    error_code=ErrorCode.CONTEXT_COMPRESSION_FAILED,
                    message=str(error),
                    run_id=None,
                )
        except Exception as e:
            logger.error("聊天处理异常: session_id=%s, run_id=%s, error=%s", session_id, run_id, e, exc_info=True)
            if run_created:
                yield RunFailedEvent(
                    run_id=run_id,
                    error_code=ErrorCode.LLM_REQUEST_FAILED,
                    message=f"Unexpected error during chat: {str(e)}",
                )
            else:
                yield RequestFailedEvent(
                    error_code=ErrorCode.LLM_REQUEST_FAILED,
                    message=f"Unexpected error during chat: {str(e)}",
                    run_id=None,
                )

    def _create_run_lock_scope(self, session_id: str, run_id: str) -> ChatRunLockScope:
        """创建聊天运行锁作用域。

        Args:
            session_id: 会话 ID
            run_id: 当前运行 ID

        Returns:
            ChatRunLockScope: 已绑定当前 session/run 的锁作用域对象
        """
        return ChatRunLockScope(
            lock_store=self._lock_store,
            session_id=session_id,
            run_id=run_id,
            ttl_seconds=self._settings.session_lock_ttl_seconds,
        )

    def _build_user_message(self, user_message: str) -> StoredMessage:
        """构造当前请求的用户消息。"""
        return StoredMessage.create(
            role="user",
            content=user_message,
            timestamp=datetime.now(timezone.utc),
        )

    def _build_running_run(self, run_id: str, session_id: str, agent: Agent, metadata: dict | None) -> Run:
        """构造待持久化的运行中 Run 对象。"""
        created_at = datetime.now(timezone.utc)  # 统一记录 run 创建时间，并复用到 updated_at，便于索引排序与后续状态更新。
        return Run(
            run_id=run_id,
            session_id=session_id,
            agent_id=agent.agent_id,
            run_type="master",
            execution_mode="foreground",
            status=RunStatus.RUNNING,
            created_at=created_at,
            updated_at=created_at,
            metadata=metadata,
        )

    def _build_unexpected_run_failed_event(self, run_id: str, error: Exception) -> RunFailedEvent:
        """把锁内的非预期异常收敛为稳定的失败终态事件。"""
        return RunFailedEvent(
            run_id=run_id,
            error_code=ErrorCode.LLM_REQUEST_FAILED,
            message=f"Unexpected error during chat: {str(error)}",
        )

    async def _consume_loop_events(
        self,
        session_id: str,
        run_id: str,
        profile: "AgentExecutionProfile",  # 执行 profile，替代旧 agent 参数
        llm_messages: list[dict],
        execution_context: ExecutionContext,
        loop_state: ConsumedLoopState,
        pending_write_buffer: PendingSessionWriteBuffer,
    ) -> AsyncIterator[Event]:
        """消费 AgentLoop 事件流并交由事件分发器处理。

        Args:
            session_id: 会话 ID，用于事件落库
            run_id: 当前运行 ID
            profile: Agent 执行配置，包含 runtime、tool_registry 等全部运行依赖
            llm_messages: 已准备好的 LLM 消息列表
            execution_context: 执行上下文
            loop_state: 由调用方提供的终态状态载体，用于保存终态事件与完整输出
        """
        try:
            async for event in self._agent_loop.run(
                run_id=run_id,  # 运行唯一标识
                profile=profile,  # Agent 执行配置，携带所有运行依赖
                messages=llm_messages,  # 已准备好的 LLM 消息列表
                session_id=session_id,  # 会话唯一标识
                context=execution_context,  # 执行上下文
            ):
                processed_event = await self._event_processor.process_event(
                    session_id=session_id,
                    event=event,
                    pending_write_buffer=pending_write_buffer,
                )

                # 先把处理器要求立即对外发出的事件转发出去。
                for outbound_event in processed_event.outbound_events:
                    yield outbound_event

                # 如果已经收到了终态，则停止继续消费后续事件。
                if processed_event.terminal_event is not None:
                    loop_state.terminal_event = processed_event.terminal_event
                    loop_state.final_output = processed_event.final_output
                    break
        except asyncio.CancelledError as e:
            # 仅当取消事件已设置时才收敛为 RunCancelledEvent；
            # 否则可能是锁心跳丢失等被动取消，应交回上层处理。
            if execution_context.cancel_event.is_set():
                loop_state.terminal_event = RunCancelledEvent(
                    run_id=run_id,
                    reason=str(e) if str(e) else "Run cancelled",
                    error_code=ErrorCode.RUN_CANCELLED,
                )
                loop_state.final_output = ""
            else:
                raise
        finally:
            await pending_write_buffer.flush()  # 无论正常结束、终态 break 还是取消退出，都要先刷完后台写链

    async def _persist_terminal_state(
        self,
        session_id: str,
        run_id: str,
        terminal_event: RunCompletedEvent | RunFailedEvent | RunCancelledEvent | None,
        final_output: str,
    ) -> None:
        """持久化终态信息。

        Args:
            session_id: 会话 ID，用于写入 assistant 成稿消息
            run_id: 当前运行 ID
            terminal_event: 终态事件，允许为空
            final_output: run_completed 对应的完整输出
        """
        if terminal_event is None:  # 没有终态时无需落库，保持与旧实现一致
            return

        finished_at = datetime.now(timezone.utc)  # 统一生成完成时间，确保 Run 与消息时间一致

        if isinstance(terminal_event, RunCompletedEvent):
            if final_output or terminal_event.reasoning_content is not None:  # 只要有用户可见正文或 reasoning_content，就必须落 assistant 消息，避免后续轮次丢失 thinking 上下文。
                assistant_message = StoredMessage.create(
                    role="assistant",
                    content=final_output or None,
                    reasoning_content=terminal_event.reasoning_content,
                    timestamp=finished_at,
                )
                pipeline = self._redis.pipeline()  # 使用同一条 pipeline 把 HSET 与 RPUSH 一次性发给 Redis
                self._run_store.queue_update_run_fields(
                    pipeline=pipeline,
                    run_id=run_id,
                    status=RunStatus.COMPLETED,
                    finished_at=finished_at,
                    output=final_output,
                )
                self._session_store.queue_append_message(
                    pipeline=pipeline,
                    session_id=session_id,
                    message=assistant_message,
                    source_run_id=run_id,
                )
                await pipeline.execute()  # 统一执行终态双写，避免两次独立 await 带来的上下文切换
                return

            await self._run_store.update_run_fields(
                run_id=run_id,
                status=RunStatus.COMPLETED,
                finished_at=finished_at,
                output=final_output,
            )
            return

        if isinstance(terminal_event, RunCancelledEvent):
            # 追加一条系统元消息，提示模型此次生成已被取消
            cancel_hint_message = StoredMessage.create(
                role="system",
                content="此次生成已被用户取消。",
                timestamp=finished_at,
                is_meta=True,
            )
            pipeline = self._redis.pipeline()  # 取消态的 Run 更新与提示消息同样无数据依赖，可合并成一次 pipeline
            self._run_store.queue_update_run_fields(
                pipeline=pipeline,
                run_id=run_id,
                status=RunStatus.CANCELLED,
                finished_at=finished_at,
                error_code=terminal_event.error_code,
                error_message=terminal_event.reason,
            )
            self._session_store.queue_append_message(
                pipeline=pipeline,
                session_id=session_id,
                message=cancel_hint_message,
                source_run_id=run_id,
            )
            await pipeline.execute()  # 一次执行取消态双写，缩小外部可观察到的短暂不一致窗口
            return

        await self._run_store.update_run_fields(
            run_id=run_id,
            status=RunStatus.FAILED,
            finished_at=finished_at,
            error_code=terminal_event.error_code,
            error_message=terminal_event.message,
        )

    def cancel_run(self, run_id: str) -> bool:
        """取消指定运行。

        Args:
            run_id: 要取消的运行 ID

        Returns:
            bool: 是否成功发送取消信号（本地触发或广播到 Redis）
        """
        local_event = self._active_cancel_events.get(run_id)
        if local_event is not None:
            logger.info(  # 记录本地命中取消，便于观察无需广播的取消路径
                "本地命中取消请求: run_id=%s, active_cancel_events=%d",
                run_id,
                len(self._active_cancel_events),
            )
            local_event.set()
            return True
        # 本地无活跃 run，则通过 Redis 广播取消信号给其他 worker
        logger.info(  # 记录广播取消路径，便于与本地命中区分
            "广播取消请求: run_id=%s, active_cancel_events=%d",
            run_id,
            len(self._active_cancel_events),
        )
        task = asyncio.create_task(self._redis.publish(f"run_cancel:{run_id}", "cancel"))
        task.add_done_callback(lambda t: t.exception() if t.done() else None)
        return True

    async def _listen_cancel_messages(self, pubsub: object) -> None:
        """监听全局模式订阅消息，并分发到本地活跃 run 的取消事件。"""
        try:
            async for message in pubsub.listen():  # 持续消费共享 pubsub 长连接上的所有取消广播
                if message.get("type") != "pmessage":  # 只处理模式订阅投递的实际消息，忽略 subscribe/pong 等控制帧
                    continue
                if message.get("data") != "cancel":  # 当前协议只约定 cancel 指令，其余消息一律忽略
                    continue

                run_id = self._extract_run_id_from_cancel_channel(message.get("channel"))  # 从频道名解析目标 run_id
                if run_id is None:  # 非法频道名直接跳过，避免错误消息污染主链路
                    continue

                cancel_event = self._active_cancel_events.get(run_id)  # 只处理当前 worker 正在执行的本地 run
                if cancel_event is not None:
                    logger.info(  # 记录 pubsub 命中本地 run 的取消，便于核对共享监听器是否正常工作
                        "取消监听命中本地 run: run_id=%s, active_cancel_events=%d",
                        run_id,
                        len(self._active_cancel_events),
                    )
                    cancel_event.set()  # 命中本地 run 时触发取消，AgentLoop 会在后续协程切点感知到该事件
        except asyncio.CancelledError:  # shutdown 主动取消监听任务属于正常关闭路径
            raise
        except Exception as error:  # noqa: BLE001 - 监听器异常只影响跨 worker 取消，不应拖垮聊天主链路
            logger.error("全局取消监听器异常退出: error=%s", error, exc_info=True)

    def _extract_run_id_from_cancel_channel(self, channel: object) -> str | None:
        """从 run_cancel 频道名中解析 run_id。"""
        if isinstance(channel, bytes):  # 非 decode_responses 场景下可能仍返回 bytes，这里统一解码后处理
            channel = channel.decode()
        if not isinstance(channel, str):  # 无法识别的频道类型直接忽略，避免异常打断监听循环
            return None
        if not channel.startswith(self._cancel_channel_prefix):  # 只解析约定前缀的频道，保证协议边界清晰
            return None

        run_id = channel.removeprefix(self._cancel_channel_prefix)  # 去掉固定前缀后，剩余部分就是目标 run_id
        return run_id or None  # 空 run_id 视为非法频道，返回 None 让上游跳过
