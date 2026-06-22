"""ChatService 实现。

提供聊天主链路编排，并将锁管理与事件处理委托给独立协作者。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, AsyncIterator
import uuid

from app.core.models.execution_context import ExecutionContext
from app.core.models.event import Event, RequestFailedEvent, RunCompletedEvent, RunFailedEvent, RunCancelledEvent
from app.core.models.error import ErrorCode
from app.core.models.stored_message import StoredMessage
from app.core.models.run import Run, RunStatus
from app.core.runtime.context_builder import (
    ContextBuilder,
    ContextCompressionError,
    NoTrimPolicy,
    SummaryPersistenceTarget,
)
from app.services.chat_event_processor import ChatEventProcessor, PendingSessionWriteBuffer
from app.services.chat_run_lock import (
    ChatRunLockHeartbeatLostError,
    ChatRunLockNotAcquiredError,
    ChatRunLockScope,
)
from app.core.ports.transactions import MainRunTerminalWrite, RunCreateWrite

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.core.models.agent import Agent, AgentExecutionProfile
    from app.core.runtime.context_builder import ContextTrimPolicy
    from app.core.ports.cancellation import RunCancelBus
    from app.core.ports.stores import LockStore, RunStore, SessionStore
    from app.core.ports.transactions import StoreTransaction
    from app.services.agent_provider import AgentProvider
    from app.core.loop.agent_loop import AgentLoop
    from app.config import Settings


@dataclass(slots=True)
class ConsumedLoopState:
    """AgentLoop 事件消费状态。

    因为 ChatService 既要一边向外流式 yield 事件，
    又要在循环结束后拿到终态结果，所以使用显式状态载体保存终态信息。
    """

    terminal_event: RunCompletedEvent | RunFailedEvent | RunCancelledEvent | None = None
    final_output: str = ""


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

    def __init__(
        self,
        session_store: "SessionStore",
        run_store: "RunStore",
        lock_store: "LockStore",
        store_transaction: "StoreTransaction",
        run_cancel_bus: "RunCancelBus",
        agent_provider: "AgentProvider",
        agent_loop: "AgentLoop",
        settings: "Settings",
        context_trim_policy: "ContextTrimPolicy | None" = None,
        event_processor: "ChatEventProcessor | None" = None,
    ) -> None:
        self._session_store = session_store
        self._run_store = run_store
        self._lock_store = lock_store
        self._store_transaction = store_transaction
        self._run_cancel_bus = run_cancel_bus
        self._agent_provider = agent_provider
        self._agent_loop = agent_loop
        self._settings = settings
        self._context_trim_policy = context_trim_policy or NoTrimPolicy()
        self._event_processor = event_processor or ChatEventProcessor(session_store)
        self._active_cancel_events: dict[str, asyncio.Event] = {}
        self._cancel_listener_task: asyncio.Task[None] | None = None
        self._cancel_listener_lock = asyncio.Lock()
        self._cancel_listener_closed = False

    async def start_cancel_listener(self) -> bool:
        """启动全局 run_cancel 监听器。"""
        if self._cancel_listener_closed:
            logger.warning("取消监听器已关闭，忽略启动请求")
            return False

        if self._cancel_listener_task is not None and not self._cancel_listener_task.done():
            return True

        async with self._cancel_listener_lock:
            if self._cancel_listener_closed:
                logger.warning("取消监听器已关闭，忽略启动请求")
                return False

            if self._cancel_listener_task is not None and not self._cancel_listener_task.done():
                return True

            self._cancel_listener_task = asyncio.create_task(
                self._listen_cancel_messages(),
                name="chat-service-run-cancel-listener",
            )
            logger.info("全局取消监听器已启动")
            return True

    async def aclose(self) -> None:
        """关闭 ChatService 自身持有的后台资源。"""
        async with self._cancel_listener_lock:
            self._cancel_listener_closed = True
            listener_task = self._cancel_listener_task
            self._cancel_listener_task = None

        if listener_task is not None:
            listener_task.cancel()
            try:
                await listener_task
            except asyncio.CancelledError:
                pass

        await self._run_cancel_bus.aclose()

    async def stream_chat(
        self,
        session_id: str,
        user_message: str,
        metadata: dict | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncIterator[Event]:
        """执行流式聊天。"""
        logger.info("开始流式聊天: session_id=%s", session_id)

        # Step 1: 检查会话是否存在
        session = await self._session_store.get_session(session_id)
        if session is None:
            logger.error("会话不存在: session_id=%s", session_id)
            yield RequestFailedEvent(
                error_code=ErrorCode.SESSION_NOT_FOUND,
                message=f"Session {session_id} not found",
            )
            return

        # Step 2: 生成 run_id
        run_id = str(uuid.uuid4())
        logger.info("生成 Run ID: run_id=%s", run_id)
        terminal_event: RunCompletedEvent | RunFailedEvent | RunCancelledEvent | None = None
        final_output = ""
        run_created = False

        # 幂等确保 pubsub 监听器已启动
        await self.start_cancel_listener()

        try:
            # Step 3: 进入聊天运行锁作用域
            async with self._create_run_lock_scope(session_id=session_id, run_id=run_id):
                effective_cancel_event = cancel_event if cancel_event is not None else asyncio.Event()
                self._active_cancel_events[run_id] = effective_cancel_event
                pending_write_buffer = self._event_processor.create_pending_write_buffer(run_id=run_id)
                logger.info(
                    "登记本地取消事件: run_id=%s, active_cancel_events=%d",
                    run_id,
                    len(self._active_cancel_events),
                )
                try:
                    # Step 4: 准备上下文：用户消息、Agent、历史、LLM 消息和 Run
                    user_message_model = self._build_user_message(user_message)
                    profile = self._agent_provider.get_default_profile()
                    agent = profile.agent
                    history, history_indices = await self._session_store.list_main_active_messages_with_indices(session_id)
                    context_build_result = await ContextBuilder.build_llm_messages_with_repair_meta(
                        agent=agent,
                        history=history,
                        history_indices=history_indices,
                        current_user_message=user_message_model,
                        trim_policy=self._context_trim_policy,
                        session_id=session_id,
                        summary_target=SummaryPersistenceTarget.for_main(session_id),
                        extra_system_messages=list(profile.extra_system_messages),
                    )
                    llm_messages = context_build_result.llm_messages
                    if context_build_result.history_dirty:
                        await self._session_store.mark_main_history_dirty(session_id)

                    # Step 5: 持久化用户消息
                    await self._session_store.append_main_message(
                        session_id=session_id,
                        message=user_message_model,
                        source_run_id=run_id,
                    )

                    # Step 6: 创建并持久化 RUNNING 状态的 Run 记录
                    run = self._build_running_run(
                        run_id=run_id,
                        session_id=session_id,
                        agent=agent,
                        metadata=metadata,
                    )
                    await self._store_transaction.create_run_and_index_session(
                        RunCreateWrite(
                            session_id=session_id,
                            run=run,
                            run_ttl_seconds=self._settings.run_ttl_seconds,
                        )
                    )
                    run_created = True

                    # Step 7: 构造执行上下文
                    execution_context = ExecutionContext(
                        run_id=run_id,
                        session_id=session_id,
                        metadata=metadata,
                        agent=agent,
                        cancel_event=effective_cancel_event,
                        run_type="master",
                    )

                    # Step 8: 处理 AgentLoop 事件流
                    loop_state = ConsumedLoopState()
                    try:
                        async for outbound_event in self._consume_loop_events(
                            session_id=session_id,
                            run_id=run_id,
                            profile=profile,
                            llm_messages=llm_messages,
                            execution_context=execution_context,
                            loop_state=loop_state,
                            pending_write_buffer=pending_write_buffer,
                        ):
                            yield outbound_event
                    except Exception as error:
                        # 后台写链 flush 失败也会从这里冒泡，必须在锁内收敛为失败态
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

                    terminal_event = loop_state.terminal_event
                    final_output = loop_state.final_output

                    # Step 9: 锁内完成终态落库
                    await self._persist_terminal_state(
                        session_id=session_id,
                        run_id=run_id,
                        terminal_event=terminal_event,
                        final_output=final_output,
                    )
                finally:
                    removed_event = self._active_cancel_events.pop(run_id, None)
                    if removed_event is not None:
                        logger.info(
                            "移除本地取消事件: run_id=%s, active_cancel_events=%d",
                            run_id,
                            len(self._active_cancel_events),
                        )
                    # 给外部 monitor 一个设置 cancel_event 的时间差窗口
                    if terminal_event is None and not effective_cancel_event.is_set():
                        try:
                            await asyncio.wait_for(effective_cancel_event.wait(), timeout=0.5)
                        except asyncio.TimeoutError:
                            pass
                    # 若客户端断开导致取消，补持久化 CANCELLED 状态
                    if terminal_event is None and effective_cancel_event.is_set():
                        await pending_write_buffer.flush()
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

            # Step 10: 锁释放后终态事件发出
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
            if run_created:
                await self._persist_terminal_state(
                    session_id=session_id,
                    run_id=run_id,
                    terminal_event=terminal_event,
                    final_output="",
                )
            yield terminal_event
        except ChatRunLockNotAcquiredError:
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
        """创建聊天运行锁作用域。"""
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
        created_at = datetime.now(timezone.utc)
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
        profile: "AgentExecutionProfile",
        llm_messages: list[dict],
        execution_context: ExecutionContext,
        loop_state: ConsumedLoopState,
        pending_write_buffer: PendingSessionWriteBuffer,
    ) -> AsyncIterator[Event]:
        """消费 AgentLoop 事件流并交由事件分发器处理。"""
        try:
            async for event in self._agent_loop.run(
                run_id=run_id,
                profile=profile,
                messages=llm_messages,
                session_id=session_id,
                context=execution_context,
            ):
                processed_event = await self._event_processor.process_event(
                    session_id=session_id,
                    event=event,
                    pending_write_buffer=pending_write_buffer,
                )

                for outbound_event in processed_event.outbound_events:
                    yield outbound_event

                if processed_event.terminal_event is not None:
                    loop_state.terminal_event = processed_event.terminal_event
                    loop_state.final_output = processed_event.final_output
                    break
        except asyncio.CancelledError as e:
            # 仅当取消事件已设置时才收敛为 RunCancelledEvent；
            # 否则可能是锁心跳丢失等被动取消，应交回上层处理
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
            await pending_write_buffer.flush()

    async def _persist_terminal_state(
        self,
        session_id: str,
        run_id: str,
        terminal_event: RunCompletedEvent | RunFailedEvent | RunCancelledEvent | None,
        final_output: str,
    ) -> None:
        """持久化终态信息。"""
        if terminal_event is None:
            return

        finished_at = datetime.now(timezone.utc)

        if isinstance(terminal_event, RunCompletedEvent):
            if final_output or terminal_event.reasoning_content is not None:
                assistant_message = StoredMessage.create(
                    role="assistant",
                    content=final_output or None,
                    reasoning_content=terminal_event.reasoning_content,
                    timestamp=finished_at,
                )
                await self._store_transaction.persist_main_run_terminal(
                    MainRunTerminalWrite(
                        session_id=session_id,
                        run_id=run_id,
                        status=RunStatus.COMPLETED,
                        finished_at=finished_at,
                        output=final_output,
                        terminal_message=assistant_message,
                    )
                )
                return

            await self._run_store.update_run_fields(
                run_id=run_id,
                status=RunStatus.COMPLETED,
                finished_at=finished_at,
                output=final_output,
            )
            return

        if isinstance(terminal_event, RunCancelledEvent):
            cancel_hint_message = StoredMessage.create(
                role="system",
                content="此次生成已被用户取消。",
                timestamp=finished_at,
                is_meta=True,
            )
            await self._store_transaction.persist_main_run_terminal(
                MainRunTerminalWrite(
                    session_id=session_id,
                    run_id=run_id,
                    status=RunStatus.CANCELLED,
                    finished_at=finished_at,
                    error_code=terminal_event.error_code,
                    error_message=terminal_event.reason,
                    terminal_message=cancel_hint_message,
                )
            )
            return

        await self._run_store.update_run_fields(
            run_id=run_id,
            status=RunStatus.FAILED,
            finished_at=finished_at,
            error_code=terminal_event.error_code,
            error_message=terminal_event.message,
        )

    def cancel_run(self, run_id: str) -> bool:
        """取消指定运行。"""
        local_event = self._active_cancel_events.get(run_id)
        if local_event is not None:
            logger.info(
                "本地命中取消请求: run_id=%s, active_cancel_events=%d",
                run_id,
                len(self._active_cancel_events),
            )
            local_event.set()
            return True
        logger.info(
            "广播取消请求: run_id=%s, active_cancel_events=%d",
            run_id,
            len(self._active_cancel_events),
        )
        task = asyncio.create_task(self._run_cancel_bus.publish_cancel(run_id))
        task.add_done_callback(lambda t: t.exception() if t.done() else None)
        return True

    async def _listen_cancel_messages(self) -> None:
        """监听全局取消广播，并分发到本地活跃 run 的取消事件。"""
        try:
            async for run_id in self._run_cancel_bus.listen_cancelled_run_ids():
                cancel_event = self._active_cancel_events.get(run_id)
                if cancel_event is not None:
                    logger.info(
                        "取消监听命中本地 run: run_id=%s, active_cancel_events=%d",
                        run_id,
                        len(self._active_cancel_events),
                    )
                    cancel_event.set()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            logger.error("全局取消监听器异常退出: error=%s", error, exc_info=True)
