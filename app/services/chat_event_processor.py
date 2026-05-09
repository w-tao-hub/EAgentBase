"""聊天事件分发器。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from typing import Awaitable, Callable, TYPE_CHECKING

from app.core.models.event import (
    AssistantWithToolsEvent,
    Event,
    MessageDeltaEvent,
    RunCancelledEvent,
    RunCompletedEvent,
    RunFailedEvent,
    RunStartedEvent,
    ToolUseCompletedEvent,
    ToolUseStartedEvent,
)
from app.core.models.stored_message import StoredMessage

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.infra.store.redis_session_store import RedisSessionStore


@dataclass(slots=True)
class ProcessedChatEvent:
    """单个事件处理结果。"""

    outbound_events: list[Event] = field(default_factory=list)
    terminal_event: RunCompletedEvent | RunFailedEvent | RunCancelledEvent | None = None
    final_output: str = ""


class PendingSessionWriteBuffer:
    """单个 run 内的会话消息后台串行写缓冲器。

    该缓冲器用于把 Redis 消息写入改成后台链式任务：
    - 多次 `enqueue()` 会按调用顺序串行执行
    - `flush()` 会等待当前已排队的所有写入完成
    - 任意一次后台写失败后，会在下一次 `enqueue()` 或 `flush()` 时把异常抛回主流程
    """

    def __init__(self, session_store: RedisSessionStore, run_id: str) -> None:
        self._session_store = session_store
        self._run_id = run_id
        self._tail_task: asyncio.Task[None] | None = None
        self._error: BaseException | None = None

    def enqueue(self, session_id: str, messages: list[StoredMessage], child_id: str | None = None) -> None:
        """把一批消息按顺序排入后台写链。"""
        self.enqueue_after_write(session_id, messages, child_id=child_id)

    def enqueue_after_write(
        self,
        session_id: str,
        messages: list[StoredMessage],
        child_id: str | None = None,
        after_write: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """把一批消息按顺序排入后台写链，并在全部写入成功后执行回调。"""
        self._raise_if_failed()
        previous_tail = self._tail_task

        async def write_messages() -> None:
            if previous_tail is not None:
                await previous_tail
            self._raise_if_failed()
            for message in messages:
                await self._session_store.append_main_message(
                    session_id=session_id,
                    message=message,
                    source_run_id=self._run_id,
                    child_id=(child_id if message.role == "tool" else None),
                )
            if after_write is not None:
                await after_write()

        task = asyncio.create_task(
            write_messages(),
            name=f"chat-event-persist:{session_id}:{self._run_id}",
        )
        task.add_done_callback(self._capture_task_error)
        self._tail_task = task

    async def flush(self) -> None:
        """等待当前已排队的全部后台写入完成。"""
        self._raise_if_failed()
        tail_task = self._tail_task
        if tail_task is None:
            return
        try:
            await tail_task
        finally:
            if self._tail_task is tail_task:
                self._tail_task = None
        self._raise_if_failed()

    def _capture_task_error(self, task: asyncio.Task[None]) -> None:
        """记录后台任务异常，避免其在事件循环中静默丢失。"""
        if task.cancelled():
            return
        try:
            task.result()
        except BaseException as error:  # noqa: BLE001
            if self._error is None:
                self._error = error

    def _raise_if_failed(self) -> None:
        """若后台写链已失败，则把原始异常抛回主流程。"""
        if self._error is not None:
            raise self._error


class ChatEventProcessor:
    """聊天事件分发器。

    该对象只关心"事件该如何被处理"，不关心完整业务编排。
    ChatService 只需把 AgentLoop 产出的事件交给它，
    然后根据处理结果决定是否继续推进主流程即可。
    """

    def __init__(self, session_store: RedisSessionStore, child_runner=None) -> None:
        self._session_store = session_store
        self._child_runner = child_runner

    def create_pending_write_buffer(self, run_id: str) -> PendingSessionWriteBuffer:
        """为单个 run 创建独立的后台写缓冲器。"""
        return PendingSessionWriteBuffer(session_store=self._session_store, run_id=run_id)

    async def process_event(
        self,
        session_id: str,
        event: Event,
        pending_write_buffer: PendingSessionWriteBuffer | None = None,
    ) -> ProcessedChatEvent:
        """处理单个聊天事件。

        Args:
            session_id: 当前会话 ID，用于写入会话历史
            event: AgentLoop 发出的原始事件
            pending_write_buffer: 单个 run 的后台写缓冲器；未提供时退化为同步落库

        Returns:
            ProcessedChatEvent: 包含需要对外转发的事件与终态收集结果
        """
        # 运行开始事件：直接对外转发，不做额外持久化。
        if isinstance(event, RunStartedEvent):
            return ProcessedChatEvent(outbound_events=[event])

        # 文本增量事件：直接对外转发。
        if isinstance(event, MessageDeltaEvent):
            return ProcessedChatEvent(outbound_events=[event])

        # 带工具调用的 assistant 消息：只写入会话历史，不直接发给客户端。
        if isinstance(event, AssistantWithToolsEvent):
            assistant_message = StoredMessage.create(
                role="assistant",
                content=event.content,
                tool_calls=event.tool_calls,
                reasoning_content=event.reasoning_content,
                timestamp=datetime.now(timezone.utc),
            )
            if pending_write_buffer is None:
                await self._session_store.append_main_message(
                    session_id=session_id,
                    message=assistant_message,
                    source_run_id=event.run_id,
                )
            else:
                pending_write_buffer.enqueue(session_id, [assistant_message])
            logger.debug(
                "已存储带 tool_calls 的 assistant 消息: session_id=%s, tool_calls_count=%d",
                session_id,
                len(event.tool_calls),
            )
            return ProcessedChatEvent()

        # 工具调用开始事件：仅转发给客户端，不写历史。
        if isinstance(event, ToolUseStartedEvent):
            return ProcessedChatEvent(outbound_events=[event])

        # 工具调用完成事件：既要把 tool 消息写入历史，也要继续转发给客户端。
        if isinstance(event, ToolUseCompletedEvent):
            tool_message = StoredMessage.create(
                role="tool",
                content=event.result,
                tool_call_id=event.tool_call_id,
                name=event.tool_name,
                timestamp=datetime.now(timezone.utc),
            )
            messages_to_persist = [tool_message]
            if event.stored_message is not None:
                messages_to_persist.append(event.stored_message)
            task_child_id = event.task_child_id if event.tool_name == "Task" else None
            if pending_write_buffer is None:
                for message in messages_to_persist:
                    await self._session_store.append_main_message(
                        session_id=session_id,
                        message=message,
                        source_run_id=event.run_id,
                        child_id=(task_child_id if message.role == "tool" else None),
                    )
            else:
                pending_write_buffer.enqueue_after_write(
                    session_id,
                    messages_to_persist,
                    child_id=task_child_id,
                )
            logger.debug(
                "已存储 tool 消息: session_id=%s, tool_call_id=%s",
                session_id,
                event.tool_call_id,
            )
            return ProcessedChatEvent(outbound_events=[event])

        # 运行完成事件：先收集终态，由 ChatService 在统一时机落库并发出。
        if isinstance(event, RunCompletedEvent):
            return ProcessedChatEvent(
                terminal_event=event,
                final_output=event.output,
            )

        # 运行失败事件：同样只收集终态，由 ChatService 统一处理。
        if isinstance(event, RunFailedEvent):
            return ProcessedChatEvent(terminal_event=event)

        # 运行取消事件：收集终态，由 ChatService 统一落库并发出。
        if isinstance(event, RunCancelledEvent):
            return ProcessedChatEvent(terminal_event=event)

        # 未识别事件：不转发也不抛错，只记录日志。
        logger.warning(
            "收到未识别的聊天事件，已忽略: session_id=%s, run_id=%s, event_type=%s",
            session_id,
            getattr(event, "run_id", None),
            type(event).__name__,
        )
        return ProcessedChatEvent()
