"""事件模型单元测试。

测试 ToolUseStartedEvent 和 ToolUseCompletedEvent 的 event_name 和 to_payload 方法。
"""

from datetime import datetime, timezone

import pytest

from app.core.models.event import (
    ToolUseCompletedEvent,
    ToolUseStartedEvent,
)
from app.core.models.stored_message import StoredMessage


class TestToolUseStartedEvent:
    """ToolUseStartedEvent 测试类。"""

    def test_event_name(self):
        """测试 event_name 属性返回正确值。"""
        # 创建事件实例
        event = ToolUseStartedEvent(
            run_id="run_123",  # 运行 ID
            tool_name="search",  # 工具名称
            tool_call_id="call_456",  # 工具调用 ID
            tool_input={"query": "test"},  # 工具输入参数
        )
        # 验证事件名称正确
        assert event.event_name == "tool_use_started"

    def test_to_payload_basic(self):
        """测试 to_payload 方法返回正确结构。"""
        # 创建事件实例
        event = ToolUseStartedEvent(
            run_id="run_123",
            tool_name="search",
            tool_call_id="call_456",
            tool_input={"query": "test"},
        )
        # 获取 payload
        payload = event.to_payload()
        # 验证 payload 结构
        assert payload["type"] == "tool_use_started"
        assert payload["run_id"] == "run_123"
        assert payload["tool_name"] == "search"
        assert payload["tool_call_id"] == "call_456"
        assert payload["tool_input"] == {"query": "test"}

    def test_to_payload_with_content(self):
        """测试带 content 字段的 to_payload。"""
        # 创建带 content 的事件实例
        event = ToolUseStartedEvent(
            run_id="run_123",
            tool_name="search",
            tool_call_id="call_456",
            tool_input={"query": "test"},
            content="additional context",  # 额外内容
        )
        # 获取 payload
        payload = event.to_payload()
        # 验证 content 字段
        assert payload["content"] == "additional context"

    def test_to_payload_without_content(self):
        """测试不带 content 字段的 to_payload。"""
        # 创建不带 content 的事件实例
        event = ToolUseStartedEvent(
            run_id="run_123",
            tool_name="search",
            tool_call_id="call_456",
            tool_input={"query": "test"},
        )
        # 获取 payload
        payload = event.to_payload()
        # 验证 content 字段为 None
        assert payload["content"] is None


class TestToolUseCompletedEvent:
    """ToolUseCompletedEvent 测试类。"""

    def test_event_name(self):
        """测试 event_name 属性返回正确值。"""
        # 创建事件实例
        event = ToolUseCompletedEvent(
            run_id="run_123",
            tool_name="search",
            tool_call_id="call_456",
            is_error=False,  # 非错误结果
            result="search results",  # 执行结果
        )
        # 验证事件名称正确
        assert event.event_name == "tool_use_completed"

    def test_to_payload_success(self):
        """测试成功结果的 to_payload。"""
        # 创建成功事件实例
        event = ToolUseCompletedEvent(
            run_id="run_123",
            tool_name="search",
            tool_call_id="call_456",
            is_error=False,
            result="search results",
        )
        # 获取 payload
        payload = event.to_payload()
        # 验证 payload 结构
        assert payload["type"] == "tool_use_completed"
        assert payload["run_id"] == "run_123"
        assert payload["tool_name"] == "search"
        assert payload["tool_call_id"] == "call_456"
        assert payload["is_error"] is False
        assert payload["result"] == "search results"

    def test_to_payload_error(self):
        """测试错误结果的 to_payload。"""
        # 创建错误事件实例
        event = ToolUseCompletedEvent(
            run_id="run_123",
            tool_name="search",
            tool_call_id="call_456",
            is_error=True,  # 错误结果
            result="error: tool not found",
        )
        # 获取 payload
        payload = event.to_payload()
        # 验证 is_error 为 True
        assert payload["is_error"] is True
        # 验证结果内容
        assert payload["result"] == "error: tool not found"

    def test_to_payload_hides_internal_stored_message(self):
        """测试内部 stored_message 字段不会泄露到公开 payload。"""
        event = ToolUseCompletedEvent(
            run_id="run_123",
            tool_name="skill",
            tool_call_id="call_456",
            is_error=False,
            result="技能加载完成",
            stored_message=StoredMessage.create(
                role="user",
                content="<skill_name>demo</skill_name>",
                timestamp=datetime(2026, 4, 10, tzinfo=timezone.utc),
                is_meta=True,
            ),
        )

        payload = event.to_payload()

        assert "stored_message" not in payload
