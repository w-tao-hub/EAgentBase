"""历史视图重建测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.models.stored_message import StoredMessage
from app.core.runtime.context_history_view import ContextHistoryViewBuilder
from app.core.ports.stores import ContextSummaryState


def _message(role: str, content: str) -> StoredMessage:
    """构造测试消息，减少样板。

    Args:
        role: 消息角色（user / assistant）。
        content: 消息正文。

    Returns:
        StoredMessage: 构造好的测试消息实例。
    """
    return StoredMessage.create(
        role=role,
        content=content,
        timestamp=datetime(2026, 5, 6, 10, 0, 0, tzinfo=timezone.utc),
        is_meta=content.startswith("<context_summary>"),
    )


def test_history_view_builder_returns_full_history_when_summary_state_missing() -> None:
    """未压缩过时，完整历史和活动窗口应完全一致。"""
    # 构造两组简单的历史消息。
    history = [_message("user", "第一轮问题"), _message("assistant", "第一轮回答")]

    # 未传入摘要状态时，活动窗口应等于完整历史。
    view = ContextHistoryViewBuilder.from_history(
        history=history,
        summary_state=None,
    )

    # 验证完整历史内容与索引。
    assert [message.content for message in view.source_history_messages] == ["第一轮问题", "第一轮回答"]
    assert view.source_history_indices == [0, 1]
    # 验证活动窗口与完整历史一致。
    assert [message.content for message in view.active_history_messages] == ["第一轮问题", "第一轮回答"]
    assert view.active_history_indices == [0, 1]


def test_history_view_builder_places_summary_first_and_keeps_absolute_indices() -> None:
    """已压缩历史重建活动窗口时，摘要必须在首位且活动索引保持绝对值。"""
    # 构造完整历史：含早期对话（将被摘要压缩）和后续对话。
    summary_message = _message("user", "<context_summary>摘要</context_summary>")
    second_question = _message("user", "第二轮问题")
    second_answer = _message("assistant", "第二轮回答")
    third_question = _message("user", "第三轮问题")
    history = [
        _message("user", "第一轮问题"),
        _message("assistant", "第一轮回答"),
        second_question,
        second_answer,
        third_question,
        summary_message,
    ]

    # 摘要状态：摘要消息在索引 5，
    # 活动窗口起点为 second_question（索引 2，早于摘要索引）。
    summary_state = ContextSummaryState(
        summary_message_id=summary_message.message_id,
        active_start_message_id=second_question.message_id,
        summary_offset=5,
        active_start_offset=2,
    )

    # 重建活动窗口。
    view = ContextHistoryViewBuilder.from_history(
        history=history,
        summary_state=summary_state,
    )

    # 验证活动窗口消息内容：摘要先行，然后是第二轮和第三轮。
    assert [message.content for message in view.active_history_messages] == [
        "<context_summary>摘要</context_summary>",
        "第二轮问题",
        "第二轮回答",
        "第三轮问题",
    ]
    # 验证活动窗口索引保持绝对值：摘要=5，第二轮问题=2，第二轮回答=3，第三轮问题=4。
    assert view.active_history_indices == [5, 2, 3, 4]


def test_history_view_builder_falls_back_to_full_history_when_summary_not_found() -> None:
    """摘要消息未在历史中找到时，应回退到完整历史。"""
    # 构造历史：不含摘要的普通消息列表。
    history = [_message("user", "第一轮问题"), _message("assistant", "第一轮回答")]
    # 摘要状态引用了不存在的 message_id。
    missing_summary_id = "00000000-0000-0000-0000-000000000000"
    summary_state = ContextSummaryState(
        summary_message_id=missing_summary_id,
        active_start_message_id=None,
        summary_offset=None,
        active_start_offset=None,
    )

    # 重建活动窗口。
    view = ContextHistoryViewBuilder.from_history(
        history=history,
        summary_state=summary_state,
    )

    # 未找到摘要时，活动窗口应等于完整历史。
    assert [message.content for message in view.active_history_messages] == ["第一轮问题", "第一轮回答"]
    assert view.active_history_indices == [0, 1]


def test_history_view_builder_handles_empty_history() -> None:
    """传入空历史列表时，所有字段应返回空列表。"""
    view = ContextHistoryViewBuilder.from_history(
        history=[],
        summary_state=None,
    )

    assert view.source_history_messages == []
    assert view.source_history_indices == []
    assert view.active_history_messages == []
    assert view.active_history_indices == []


def test_history_view_builder_starts_from_summary_when_active_start_missing() -> None:
    """active_start_message_id 为 None 时，活动窗口只包含摘要及其之后的消息。"""
    summary_message = _message("user", "<context_summary>摘要</context_summary>")
    history = [
        _message("user", "旧问题"),
        _message("assistant", "旧回答"),
        summary_message,
    ]

    # 摘要状态中 active_start_message_id 为 None，表示无保留窗口。
    summary_state = ContextSummaryState(
        summary_message_id=summary_message.message_id,
        active_start_message_id=None,
        summary_offset=2,
        active_start_offset=None,
    )

    view = ContextHistoryViewBuilder.from_history(
        history=history,
        summary_state=summary_state,
    )

    # 验证活动窗口只包含摘要及其之后的消息。
    assert [message.content for message in view.active_history_messages] == [
        "<context_summary>摘要</context_summary>",
    ]
    assert view.active_history_indices == [2]


def test_history_view_builder_falls_back_when_active_start_after_summary() -> None:
    """active_start_message_id 位于摘要消息之后时，起点非法，应回退到完整历史。"""
    summary_message = _message("user", "<context_summary>摘要</context_summary>")
    history = [
        _message("user", "第一轮问题"),
        _message("assistant", "第一轮回答"),
        summary_message,
        _message("user", "后续问题"),
    ]

    # active_start_message_id 指向摘要之后的消息，表示起点位置非法。
    summary_state = ContextSummaryState(
        summary_message_id=summary_message.message_id,
        active_start_message_id=history[-1].message_id,
        summary_offset=2,
        active_start_offset=3,
    )

    view = ContextHistoryViewBuilder.from_history(
        history=history,
        summary_state=summary_state,
    )

    # 应回退到完整历史。
    assert [message.content for message in view.active_history_messages] == [
        "第一轮问题",
        "第一轮回答",
        "<context_summary>摘要</context_summary>",
        "后续问题",
    ]
    assert view.active_history_indices == [0, 1, 2, 3]


def test_history_view_builder_uses_provided_indices_for_full_history() -> None:
    """传入 history_indices 时，应直接使用外部索引作为 source_history_indices 和活动窗口索引。"""
    history = [
        _message("user", "消息一"),
        _message("assistant", "消息二"),
        _message("user", "消息三"),
    ]
    # 外部绝对索引模拟来自 Redis List 的偏移。
    external_indices = [8, 12, 15]

    view = ContextHistoryViewBuilder.from_history(
        history=history,
        summary_state=None,
        history_indices=external_indices,
    )

    # source_history_indices 应以外部索引为准，而非 range(len(history))。
    assert view.source_history_indices == [8, 12, 15]
    # summary_state=None 时，活动窗口应等于完整历史。
    assert view.active_history_indices == [8, 12, 15]
    assert [m.content for m in view.active_history_messages] == ["消息一", "消息二", "消息三"]


def test_history_view_builder_ignores_summary_state_when_indices_provided() -> None:
    """传入 history_indices 后，即使 summary_state 非空，也不基于它二次重建活动窗口。"""
    history = [
        _message("user", "消息A"),
        _message("assistant", "消息B"),
    ]
    # 构造一个 summary_state，但其 message_id 在 history 中不存在。
    summary_state = ContextSummaryState(
        summary_message_id="00000000-0000-0000-0000-000000000000",
        active_start_message_id=None,
        summary_offset=None,
        active_start_offset=None,
    )

    # 传入 history_indices 后，即使 summary_state 中的 UUID 不匹配，也不能影响结果。
    view = ContextHistoryViewBuilder.from_history(
        history=history,
        summary_state=summary_state,
        history_indices=[5, 9],
    )

    assert view.source_history_indices == [5, 9]
    assert view.active_history_indices == [5, 9]
    assert [m.content for m in view.active_history_messages] == ["消息A", "消息B"]


def test_history_view_builder_raises_when_indices_length_mismatch() -> None:
    """history_indices 长度与 history 不一致时，builder 应直接报错。"""
    history = [
        _message("user", "消息A"),
        _message("assistant", "消息B"),
    ]
    with pytest.raises(ValueError, match="history_indices 长度与 history 不一致"):
        ContextHistoryViewBuilder.from_history(
            history=history,
            summary_state=None,
            history_indices=[0],  # 长度 1 != 2
        )


def test_source_history_fields_reflect_input_window_when_indices_provided() -> None:
    """传入活动窗口 + 外部绝对索引时，source_history_* 表达的是输入片段而非全量历史。"""
    history = [
        _message("user", "片段消息1"),
        _message("assistant", "片段消息2"),
    ]
    external_indices = [100, 101]  # 模拟来自 Redis List 的全局偏移

    view = ContextHistoryViewBuilder.from_history(
        history=history,
        summary_state=None,
        history_indices=external_indices,
    )

    # source_history_* 表达的是输入片段及其外部绝对索引，不是 range(len(history))。
    assert view.source_history_messages == history
    assert view.source_history_indices == [100, 101]
    # summary_state=None 时，active_history_* 应与 source_history_* 一致。
    assert view.active_history_messages == view.source_history_messages
    assert view.active_history_indices == view.source_history_indices
