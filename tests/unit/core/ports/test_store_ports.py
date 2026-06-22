"""Store 端口基础测试。"""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.ports import (
    ContextSummaryState,
    PersistedToolResult,
    SessionChildSummary,
)


def test_store_port_dtos_keep_expected_fields() -> None:
    """测试 Store 端口 DTO 字段稳定，避免实现方依赖 Redis 模块。"""
    summary_state = ContextSummaryState(
        summary_message_id="summary-1",
        active_start_message_id="message-1",
        summary_offset=3,
        active_start_offset=1,
    )
    child_summary = SessionChildSummary(
        resume_id="child-1",
        subagent_type="Worker",
        description="继续处理任务",
    )
    tool_result = PersistedToolResult(
        key="agent:tool_result:result-1",
        session_id="session-1",
        tool_name="example_tool",
        content="完整输出",
        created_at=datetime(2026, 6, 22, tzinfo=timezone.utc),
        content_length=4,
    )

    assert summary_state.summary_message_id == "summary-1"
    assert child_summary.resume_id == "child-1"
    assert tool_result.content == "完整输出"
