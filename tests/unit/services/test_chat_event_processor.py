"""ChatEventProcessor 单元测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app.core.models.event import (
    AssistantWithToolsEvent,
    ExternalEvent,
    RunCompletedEvent,
    RunFailedEvent,
    ToolUseCompletedEvent,
    ToolUseStartedEvent,
)
from app.core.models.error import ErrorCode
from app.core.models.stored_message import StoredMessage
from app.services.chat_event_processor import ChatEventProcessor
from app.infra.store.redis_session_store import RedisSessionStore


class UnknownExternalEvent(ExternalEvent):
    """用于验证未知外部事件分支的测试事件。"""

    run_id: str

    @property
    def event_name(self) -> str:
        """返回测试事件名。"""
        return "unknown_external"

    def to_payload(self) -> dict:
        """返回最小 payload，供测试读取 run_id。"""
        return {
            "type": self.event_name,
            "run_id": self.run_id,
        }


@pytest.fixture
async def session_store(fake_redis):
    """提供测试用会话存储。"""
    return RedisSessionStore(fake_redis, key_prefix="test")  # 创建隔离的测试存储实例


@pytest.mark.asyncio
async def test_chat_event_processor_persists_assistant_with_tools_without_emitting(session_store):
    """测试带 tool_calls 的 assistant 事件只落库、不对外转发。"""
    processor = ChatEventProcessor(session_store)  # 创建待测处理器

    # 预先写入一条消息，确保后续断言容易核对顺序。
    await session_store.append_message(
        "session-1",
        StoredMessage.create(
            role="user",
            content="hi",
            timestamp=datetime.now(timezone.utc),
        ),
    )

    processed = await processor.process_event(
        session_id="session-1",
        event=AssistantWithToolsEvent(
            run_id="run-1",
            content="让我调用一下工具",
            reasoning_content="先分析一下工具是否有用",
            tool_calls=[
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "search", "arguments": "{\"q\":\"hello\"}"},
                }
            ],
        ),
    )

    assert processed.outbound_events == []  # 验证该事件不会直接发给客户端
    assert processed.terminal_event is None  # 验证该事件不是终态

    messages = await session_store.list_messages("session-1")  # 读取会话历史确认是否已落库
    assert len(messages) == 2  # 应为原始 user 消息 + 新增 assistant/tool_calls 消息
    assert messages[-1].role == "assistant"  # 验证最后一条是 assistant
    assert messages[-1].content == "让我调用一下工具"  # 验证文本内容正确
    assert messages[-1].reasoning_content == "先分析一下工具是否有用"  # 验证 reasoning_content 已持久化
    assert messages[-1].tool_calls is not None  # 验证 tool_calls 已持久化
    assert messages[-1].tool_calls[0]["id"] == "call-1"  # 验证 tool_call 标识正确


@pytest.mark.asyncio
async def test_chat_event_processor_persists_tool_message_before_emitting(session_store):
    """测试工具完成事件会先落库，再作为对外事件返回。"""
    processor = ChatEventProcessor(session_store)  # 创建待测处理器

    processed = await processor.process_event(
        session_id="session-1",
        event=ToolUseCompletedEvent(
            run_id="run-1",
            tool_name="search",
            tool_call_id="call-1",
            is_error=False,
            result="result text",
        ),
    )

    assert len(processed.outbound_events) == 1  # 验证会继续把工具完成事件对外转发
    assert processed.outbound_events[0].event_name == "tool_use_completed"  # 验证转发事件类型正确
    assert processed.terminal_event is None  # 验证工具事件不是终态

    messages = await session_store.list_messages("session-1")  # 读取会话历史确认 tool 消息已写入
    assert len(messages) == 1  # 当前只应有一条 tool 消息
    assert messages[0].role == "tool"  # 验证消息角色为 tool
    assert messages[0].content == "result text"  # 验证工具结果被写入消息内容
    assert messages[0].tool_call_id == "call-1"  # 验证 tool_call_id 已持久化
    assert messages[0].name == "search"  # 验证工具名称已持久化


@pytest.mark.asyncio
async def test_chat_event_processor_persists_internal_stored_message_after_tool_message(session_store):
    """测试工具完成事件携带 stored_message 时，会按顺序先写 tool 再写附加消息。"""
    processor = ChatEventProcessor(session_store)

    processed = await processor.process_event(
        session_id="session-1",
        event=ToolUseCompletedEvent(
            run_id="run-1",
            tool_name="skill",
            tool_call_id="call-1",
            is_error=False,
            result="技能加载完成",
            stored_message=StoredMessage.create(
                role="user",
                content="<skill_name>demo</skill_name><skill_message>full</skill_message>",
                timestamp=datetime.now(timezone.utc),
                is_meta=True,
            ),
        ),
    )

    assert len(processed.outbound_events) == 1
    assert processed.outbound_events[0].event_name == "tool_use_completed"

    messages = await session_store.list_messages("session-1")
    assert len(messages) == 2
    assert messages[0].role == "tool"
    assert messages[0].content == "技能加载完成"
    assert messages[1].role == "user"
    assert messages[1].is_meta is True
    assert messages[1].content == "<skill_name>demo</skill_name><skill_message>full</skill_message>"


@pytest.mark.asyncio
async def test_chat_event_processor_background_buffer_preserves_tool_and_stored_message_order(session_store):
    """测试启用后台写缓冲时，tool 消息与附带 stored_message 仍按原顺序落库。"""
    processor = ChatEventProcessor(session_store)
    pending_write_buffer = processor.create_pending_write_buffer(run_id="run-1")

    processed = await processor.process_event(
        session_id="session-1",
        event=ToolUseCompletedEvent(
            run_id="run-1",
            tool_name="skill",
            tool_call_id="call-1",
            is_error=False,
            result="技能加载完成",
            stored_message=StoredMessage.create(
                role="user",
                content="<skill_name>demo</skill_name><skill_message>full</skill_message>",
                timestamp=datetime.now(timezone.utc),
                is_meta=True,
            ),
        ),
        pending_write_buffer=pending_write_buffer,
    )

    assert len(processed.outbound_events) == 1  # 启用后台写缓冲后，工具完成事件仍应立即继续对外转发

    messages_before_flush = await session_store.list_messages("session-1")
    assert messages_before_flush == []  # flush 前不要求消息已经落库，避免 Redis 写阻塞 SSE 转发

    await pending_write_buffer.flush()  # 等待后台写链完成，确认最终落库顺序

    messages = await session_store.list_messages("session-1")
    assert len(messages) == 2
    assert messages[0].role == "tool"
    assert messages[0].content == "技能加载完成"
    assert messages[1].role == "user"
    assert messages[1].is_meta is True


@pytest.mark.asyncio
async def test_chat_event_processor_background_buffer_propagates_append_error(session_store, monkeypatch):
    """测试后台写缓冲中的 Redis 追加失败会在 flush 时回传给主流程。"""
    processor = ChatEventProcessor(session_store)
    pending_write_buffer = processor.create_pending_write_buffer(run_id="run-1")
    original_append = session_store.append_main_message
    write_started = asyncio.Event()  # 用于确保测试确实等到后台任务开始执行

    async def failing_append(
        session_id: str,
        message: StoredMessage,
        source_run_id: str | None = None,
        child_id: str | None = None,
    ) -> None:
        """让 tool 消息写入失败，验证 flush 会把异常重新抛出。"""
        del child_id
        write_started.set()
        if message.role == "tool":
            raise RuntimeError("tool append failed")
        await original_append(session_id, message, source_run_id=source_run_id)

    monkeypatch.setattr(session_store, "append_main_message", failing_append)

    await processor.process_event(
        session_id="session-1",
        event=ToolUseCompletedEvent(
            run_id="run-1",
            tool_name="search",
            tool_call_id="call-1",
            is_error=False,
            result="result text",
        ),
        pending_write_buffer=pending_write_buffer,
    )

    with pytest.raises(RuntimeError, match="tool append failed"):
        await pending_write_buffer.flush()


@pytest.mark.asyncio
async def test_chat_event_processor_persists_task_tool_message_with_child_id_after_background_flush(session_store):
    """测试 Task 结果写入主会话后，会保留 child_id 元数据。"""
    processor = ChatEventProcessor(session_store)
    pending_write_buffer = processor.create_pending_write_buffer(run_id="run-1")

    processed = await processor.process_event(
        session_id="session-1",
        event=ToolUseCompletedEvent(
            run_id="run-1",
            tool_name="Task",
            tool_call_id="call-1",
            is_error=False,
            result="子代理 Plan 已完成。\nchild_id: plan-1\nchild_run_id: child-run-1\n输出:\n完成",
            task_child_id="plan-1",
        ),
        pending_write_buffer=pending_write_buffer,
    )

    assert len(processed.outbound_events) == 1

    await pending_write_buffer.flush()

    messages = await session_store.list_main_messages("session-1")
    assert len(messages) == 1
    assert messages[0].role == "tool"
    assert messages[0].meta.child_id == "plan-1"
    assert messages[0].meta.subagent_type is None


@pytest.mark.asyncio
async def test_chat_event_processor_collects_terminal_events_without_emitting(session_store):
    """测试终态事件只被收集，不在处理阶段立即转发。"""
    processor = ChatEventProcessor(session_store)  # 创建待测处理器

    completed_result = await processor.process_event(
        session_id="session-1",
        event=RunCompletedEvent(run_id="run-1", output="done"),
    )
    failed_result = await processor.process_event(
        session_id="session-1",
        event=RunFailedEvent(
            run_id="run-2",
            error_code=ErrorCode.LLM_REQUEST_FAILED,
            message="boom",
        ),
    )
    started_result = await processor.process_event(
        session_id="session-1",
        event=ToolUseStartedEvent(
            run_id="run-3",
            tool_name="search",
            tool_call_id="call-3",
            tool_input={"q": "hello"},
        ),
    )

    assert completed_result.outbound_events == []  # 验证 completed 不在处理器内直接发出
    assert completed_result.terminal_event is not None  # 验证 completed 会被收集为终态
    assert completed_result.final_output == "done"  # 验证完整输出被保留下来

    assert failed_result.outbound_events == []  # 验证 failed 也不在处理器内直接发出
    assert failed_result.terminal_event is not None  # 验证 failed 会被收集为终态
    assert failed_result.terminal_event.message == "boom"  # 验证失败消息被保留

    assert len(started_result.outbound_events) == 1  # 验证工具开始事件会直接转发
    assert started_result.outbound_events[0].event_name == "tool_use_started"  # 验证转发事件类型正确


@pytest.mark.asyncio
async def test_chat_event_processor_warns_for_unknown_event(session_store, caplog):
    """测试未知事件会记录包含上下文的告警日志。"""
    processor = ChatEventProcessor(session_store)  # 创建待测处理器

    with caplog.at_level("WARNING"):
        processed = await processor.process_event(
            session_id="session-1",
            event=UnknownExternalEvent(run_id="run-external"),
        )

    assert processed.outbound_events == []  # 未知事件不应被误发给客户端
    assert processed.terminal_event is None  # 未知事件不应被误判为终态
    assert "UnknownExternalEvent" in caplog.text  # 日志中应包含事件类型
    assert "session-1" in caplog.text  # 日志中应包含会话上下文
    assert "run-external" in caplog.text  # 日志中应包含运行上下文
