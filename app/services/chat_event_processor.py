"""聊天事件分发器。

负责把 AgentLoop 产出的事件翻译成两类结果：
1. 需要立即转发给客户端的事件；
2. 需要落库或收集为终态的内部处理结果。

这样可以把原先堆在 ChatService.stream_chat() 中的
事件判断与持久化逻辑收口到独立协作者里。
"""

from __future__ import annotations  # 启用未来注解，方便类型引用自身与后置定义

import asyncio  # 导入 asyncio，用于串行后台落库任务
from dataclasses import dataclass, field  # 导入数据类工具，用于表达处理结果
from datetime import datetime, timezone  # 导入时间工具，为落库消息统一生成时间戳
import logging  # 导入标准库日志，保持 services 层不依赖 infra 日志路径
from typing import Awaitable, Callable, TYPE_CHECKING  # 导入类型检查标记，避免运行时循环依赖

from app.core.models.event import (  # 导入服务层需要识别的事件类型
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
from app.core.models.stored_message import StoredMessage  # 导入存储消息模型，用于将事件落库为历史消息

logger = logging.getLogger(__name__)  # 创建模块级日志器，记录事件处理轨迹

if TYPE_CHECKING:  # 仅在类型检查时导入，运行时避免不必要耦合
    from app.infra.store.redis_session_store import RedisSessionStore  # 会话存储类型


@dataclass(slots=True)
class ProcessedChatEvent:
    """单个事件处理结果。

    Attributes:
        outbound_events: 需要继续向上游调用方转发的事件列表
        terminal_event: 如果当前事件是终态，则在此返回终态对象
        final_output: 当终态为 run_completed 时，记录完整输出内容
    """

    outbound_events: list[Event] = field(default_factory=list)  # 保存要对外发出的事件列表
    terminal_event: RunCompletedEvent | RunFailedEvent | RunCancelledEvent | None = None  # 保存终态事件
    final_output: str = ""  # 保存 run_completed 时的完整输出文本


class PendingSessionWriteBuffer:
    """单个 run 内的会话消息后台串行写缓冲器。

    该缓冲器用于把 Redis 消息写入改成后台链式任务：
    - 多次 `enqueue()` 会按调用顺序串行执行
    - `flush()` 会等待当前已排队的所有写入完成
    - 任意一次后台写失败后，会在下一次 `enqueue()` 或 `flush()` 时把异常抛回主流程
    """

    def __init__(self, session_store: RedisSessionStore, run_id: str) -> None:
        """初始化后台写缓冲器。"""
        self._session_store = session_store  # 保存会话存储，供后台任务复用
        self._run_id = run_id  # 保存 run_id，便于后台任务命名与日志排查
        self._tail_task: asyncio.Task[None] | None = None  # 保存当前写链尾任务，确保后续写入继续串行接在尾部
        self._error: BaseException | None = None  # 保存首个后台写异常，后续统一回传给主流程

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
        self._raise_if_failed()  # 入队前先检查历史后台任务是否已经失败，避免继续堆积脏任务
        previous_tail = self._tail_task  # 记录旧尾任务，让新任务串到其后面

        async def write_messages() -> None:
            """等待前序任务完成后，顺序追加当前批次消息。"""
            if previous_tail is not None:  # 若前面已有后台写任务，则必须先等它完成，保证消息时序稳定
                await previous_tail
            self._raise_if_failed()  # 前序任务可能已经在等待期间失败，这里再次检查并及时中断
            for message in messages:  # 当前批次内部仍严格按调用顺序逐条写入 Redis
                await self._session_store.append_main_message(  # 统一写入主会话长期上下文，而不是旧的单流消息 key。
                    session_id=session_id,
                    message=message,
                    source_run_id=self._run_id,
                    child_id=(child_id if message.role == "tool" else None),
                )
            if after_write is not None:  # 只有所有主上下文消息都落库成功后，才允许执行后置动作
                await after_write()

        task = asyncio.create_task(
            write_messages(),
            name=f"chat-event-persist:{session_id}:{self._run_id}",
        )  # 创建新的后台写任务，并将其接到现有写链尾部
        task.add_done_callback(self._capture_task_error)  # 统一捕获后台失败，避免异常只落到 event loop 日志
        self._tail_task = task  # 更新链尾，供后续 enqueue/flush 继续串联

    async def flush(self) -> None:
        """等待当前已排队的全部后台写入完成。"""
        self._raise_if_failed()  # flush 前先暴露已知异常，避免误以为没有待处理错误
        tail_task = self._tail_task  # 读取当前链尾快照，确保本次 flush 等待的是已有全部任务
        if tail_task is None:  # 没有待刷新的后台写任务时直接返回
            return
        try:
            await tail_task  # 等待尾任务完成，即可保证此前所有串行任务也已经完成
        finally:
            if self._tail_task is tail_task:  # 只有当链尾未被后续 enqueue 替换时，才清空引用
                self._tail_task = None
        self._raise_if_failed()  # flush 完成后再次检查后台链是否曾经失败，确保异常回传主流程

    def _capture_task_error(self, task: asyncio.Task[None]) -> None:
        """记录后台任务异常，避免其在事件循环中静默丢失。"""
        if task.cancelled():  # 取消属于协程生命周期控制，不在此处转换为业务异常
            return
        try:
            task.result()  # 主动读取结果，阻止“Task exception was never retrieved”日志污染测试输出
        except BaseException as error:  # noqa: BLE001 - 这里需要保存原始异常并由 flush 原样抛回
            if self._error is None:  # 只保留首个异常，避免后续链式失败覆盖根因
                self._error = error

    def _raise_if_failed(self) -> None:
        """若后台写链已失败，则把原始异常抛回主流程。"""
        if self._error is not None:  # 一旦后台链失败，后续 enqueue/flush 必须立即中止并上报
            raise self._error


class ChatEventProcessor:
    """聊天事件分发器。

    该对象只关心“事件该如何被处理”，不关心完整业务编排。
    ChatService 只需把 AgentLoop 产出的事件交给它，
    然后根据处理结果决定是否继续推进主流程即可。
    """

    def __init__(self, session_store: RedisSessionStore, child_runner=None) -> None:  # 构造函数
        """初始化事件分发器。

        Args:
            session_store: 会话存储，用于把 assistant/tool 相关事件落到历史中
            child_runner: 保留兼容入参，当前版本不再消费
        """
        self._session_store = session_store  # 保存会话存储引用，供事件落库使用
        self._child_runner = child_runner  # 保留兼容字段，避免装配层同步大改

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

        # 文本增量事件：直接对外转发，保持真正流式输出体验。
        if isinstance(event, MessageDeltaEvent):
            return ProcessedChatEvent(outbound_events=[event])

        # 带工具调用的 assistant 消息：只写入会话历史，不直接发给客户端。
        if isinstance(event, AssistantWithToolsEvent):
            assistant_message = StoredMessage.create(
                role="assistant",  # 标记消息角色为 assistant
                content=event.content,  # 保存模型的思考文本，允许为空
                tool_calls=event.tool_calls,  # 保存完整 tool_calls，供后续多轮对话使用
                reasoning_content=event.reasoning_content,  # 保存 thinking 模式返回的 reasoning_content，供后续 user 轮次原样回传
                timestamp=datetime.now(timezone.utc),  # 使用当前 UTC 时间作为消息时间戳
            )
            if pending_write_buffer is None:  # 未提供后台缓冲器时，保持旧行为：同步写入 Redis
                await self._session_store.append_main_message(
                    session_id=session_id,
                    message=assistant_message,
                    source_run_id=event.run_id,
                )
            else:  # 主链路提供后台缓冲器时，只负责入队，避免阻塞后续 SSE 事件转发
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
                role="tool",  # 标记消息角色为 tool
                content=event.result,  # 保存工具执行结果
                tool_call_id=event.tool_call_id,  # 保存工具调用 ID，便于与 assistant tool_calls 对应
                name=event.tool_name,  # 保存工具名称，便于历史回放
                timestamp=datetime.now(timezone.utc),  # 使用当前 UTC 时间作为消息时间戳
            )
            messages_to_persist = [tool_message]  # 先准备本次工具事件对应的消息批次，保证写入顺序在一个 enqueue 内固定
            if event.stored_message is not None:  # 工具若附带隐藏消息，则必须紧跟 tool 消息写入同一批次
                messages_to_persist.append(event.stored_message)
            task_child_id = event.task_child_id if event.tool_name == "Task" else None  # Task 结果允许在主会话保留 child 归属
            if pending_write_buffer is None:  # 未启用后台写链时，保持旧的同步落库顺序
                for message in messages_to_persist:
                    await self._session_store.append_main_message(
                        session_id=session_id,
                        message=message,
                        source_run_id=event.run_id,
                        child_id=(task_child_id if message.role == "tool" else None),
                    )
            else:  # 启用后台写链时，把整批消息排到同一个后台任务中，确保 tool -> stored_message 顺序稳定
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
                terminal_event=event,  # 保存终态事件对象
                final_output=event.output,  # 保存完整输出，供终态落库使用
            )

        # 运行失败事件：同样只收集终态，由 ChatService 统一处理。
        if isinstance(event, RunFailedEvent):
            return ProcessedChatEvent(terminal_event=event)

        # 运行取消事件：收集终态，由 ChatService 统一落库并发出。
        if isinstance(event, RunCancelledEvent):
            return ProcessedChatEvent(terminal_event=event)

        # 未识别事件：保持与旧实现一致，不转发也不抛错，只记录日志。
        logger.warning(
            "收到未识别的聊天事件，已忽略: session_id=%s, run_id=%s, event_type=%s",
            session_id,
            getattr(event, "run_id", None),
            type(event).__name__,
        )
        return ProcessedChatEvent()
