"""摘要规划模块单元测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.models.agent import Agent
from app.core.models.stored_message import StoredMessage
from app.core.runtime.context_builder import ContextBuilder, ContextCompressionError
from app.core.runtime.context_history_view import ContextHistoryViewBuilder
from app.core.runtime.context_summary_planner import ContextSummaryPlanner
from tests.fakes import FakeLLMAdapter


def _message(role: str, content: str, *, reasoning_content: str | None = None) -> StoredMessage:
    """构造测试消息。"""
    return StoredMessage.create(
        role=role,
        content=content,
        reasoning_content=reasoning_content,
        timestamp=datetime(2026, 5, 6, 10, 0, 0, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_summary_planner_returns_none_when_below_threshold() -> None:
    """未超过 token 阈值时，规划器应跳过压缩。"""
    agent = Agent(
        agent_id="agent-1",
        name="Agent",
        model="gpt-4.1-mini",
        system_prompt="主系统提示词",
        temperature=0.2,
    )
    # prompt_token_counts=[40] 表示估算结果低于 threshold=60，应跳过压缩。
    llm_adapter = FakeLLMAdapter(prompt_token_counts=[40], completion_text="不应被调用")
    planner = ContextSummaryPlanner(llm_adapter=llm_adapter, token_threshold=60)
    history = [
        _message("user", "第一轮问题"),
        _message("assistant", "第一轮回答"),
    ]
    view = ContextHistoryViewBuilder.from_history(history=history, summary_state=None)
    snapshot = ContextBuilder.prepare_context_snapshot(
        system_message=StoredMessage.create(
            role="system",
            content="主系统提示词",
            timestamp=datetime(2026, 5, 6, 10, 0, 0, tzinfo=timezone.utc),
        ),
        extra_system_messages=None,
        history=view.active_history_messages,
        history_indices=view.active_history_indices,
        current_user_message=None,
    )

    result = await planner.plan(
        agent=agent,
        history_view=view,
        snapshot=snapshot,
    )

    assert result is None
    assert llm_adapter.last_completion_call is None


@pytest.mark.asyncio
async def test_summary_planner_returns_absolute_active_start_offset() -> None:
    """规划结果中的活动起点偏移必须是完整历史绝对索引。"""
    agent = Agent(
        agent_id="agent-1",
        name="Agent",
        model="gpt-4.1-mini",
        system_prompt="主系统提示词",
        temperature=0.2,
    )
    llm_adapter = FakeLLMAdapter(
        prompt_token_counts=[150, 150, 40],
        completion_text="压缩摘要",
    )
    planner = ContextSummaryPlanner(llm_adapter=llm_adapter, token_threshold=60)
    history = [
        _message("user", "第一轮问题"),
        _message("assistant", "第一轮回答"),
        _message("user", "第二轮问题"),
        _message("assistant", "第二轮回答"),
        _message("user", "第三轮问题"),
    ]
    view = ContextHistoryViewBuilder.from_history(history=history, summary_state=None)
    snapshot = ContextBuilder.prepare_context_snapshot(
        system_message=StoredMessage.create(
            role="system",
            content="主系统提示词",
            timestamp=datetime(2026, 5, 6, 10, 0, 0, tzinfo=timezone.utc),
        ),
        extra_system_messages=None,
        history=view.active_history_messages,
        history_indices=view.active_history_indices,
        current_user_message=None,
    )

    result = await planner.plan(
        agent=agent,
        history_view=view,
        snapshot=snapshot,
    )

    assert result is not None
    assert result.active_start_message is not None
    assert result.active_start_message.content == "第二轮问题"
    assert result.active_start_offset == 2


@pytest.mark.asyncio
async def test_summary_planner_raises_when_no_compressible_history() -> None:
    """仅有两轮历史且超过阈值时，因无旧历史可压缩而直接报错。"""
    agent = Agent(
        agent_id="agent-1",
        name="Agent",
        model="gpt-4.1-mini",
        system_prompt="主系统提示词",
        temperature=0.2,
    )
    # 两轮会话，150 > 60 触发压缩，但 keep_start=0 → 无旧历史可压缩。
    llm_adapter = FakeLLMAdapter(
        prompt_token_counts=[150, 150],
        completion_text="不应被调用",
    )
    planner = ContextSummaryPlanner(llm_adapter=llm_adapter, token_threshold=60)
    history = [
        _message("user", "第一轮问题"),
        _message("assistant", "第一轮回答"),
        _message("user", "第二轮问题"),
        _message("assistant", "第二轮回答"),
    ]
    view = ContextHistoryViewBuilder.from_history(history=history, summary_state=None)
    snapshot = ContextBuilder.prepare_context_snapshot(
        system_message=StoredMessage.create(
            role="system", content="主系统提示词",
            timestamp=datetime(2026, 5, 6, 10, 0, 0, tzinfo=timezone.utc),
        ),
        extra_system_messages=None,
        history=view.active_history_messages,
        history_indices=view.active_history_indices,
        current_user_message=None,
    )

    with pytest.raises(ContextCompressionError, match="没有可压缩历史"):
        await planner.plan(agent=agent, history_view=view, snapshot=snapshot)


@pytest.mark.asyncio
async def test_summary_planner_raises_when_summary_too_large() -> None:
    """摘要 token 超过上限 (11_000) 时直接报错。"""
    agent = Agent(
        agent_id="agent-1",
        name="Agent",
        model="gpt-4.1-mini",
        system_prompt="主系统提示词",
        temperature=0.2,
    )
    # 第三次调用返回 12_000 > 11_000 → summary_too_large
    llm_adapter = FakeLLMAdapter(
        prompt_token_counts=[150, 150, 12_000],
        completion_text="压缩摘要",
    )
    planner = ContextSummaryPlanner(llm_adapter=llm_adapter, token_threshold=60)
    history = [
        _message("user", "第一轮问题"),
        _message("assistant", "第一轮回答"),
        _message("user", "第二轮问题"),
        _message("assistant", "第二轮回答"),
        _message("user", "第三轮问题"),
    ]
    view = ContextHistoryViewBuilder.from_history(history=history, summary_state=None)
    snapshot = ContextBuilder.prepare_context_snapshot(
        system_message=StoredMessage.create(
            role="system", content="主系统提示词",
            timestamp=datetime(2026, 5, 6, 10, 0, 0, tzinfo=timezone.utc),
        ),
        extra_system_messages=None,
        history=view.active_history_messages,
        history_indices=view.active_history_indices,
        current_user_message=None,
    )

    with pytest.raises(ContextCompressionError, match="压缩摘要超过允许上限"):
        await planner.plan(agent=agent, history_view=view, snapshot=snapshot)


@pytest.mark.asyncio
async def test_summary_planner_returns_prune_only_when_cleanup_sufficient() -> None:
    """旧历史清理后 token 降到阈值以下时，返回仅裁剪不含摘要的结果。"""
    agent = Agent(
        agent_id="agent-1",
        name="Agent",
        model="gpt-4.1-mini",
        system_prompt="主系统提示词",
        temperature=0.2,
    )
    # 150 > 60 → 触发压缩；30 < cleanup_threshold(40) → 仅裁剪不摘要
    llm_adapter = FakeLLMAdapter(
        prompt_token_counts=[150, 30],
        completion_text="不应被调用",
    )
    planner = ContextSummaryPlanner(llm_adapter=llm_adapter, token_threshold=60)
    history = [
        _message("user", "第一轮问题"),
        _message("assistant", "第一轮回答"),
        _message("user", "第二轮问题"),
        _message("assistant", "第二轮回答"),
        _message("user", "第三轮问题"),
    ]
    view = ContextHistoryViewBuilder.from_history(history=history, summary_state=None)
    snapshot = ContextBuilder.prepare_context_snapshot(
        system_message=StoredMessage.create(
            role="system", content="主系统提示词",
            timestamp=datetime(2026, 5, 6, 10, 0, 0, tzinfo=timezone.utc),
        ),
        extra_system_messages=None,
        history=view.active_history_messages,
        history_indices=view.active_history_indices,
        current_user_message=None,
    )

    result = await planner.plan(agent=agent, history_view=view, snapshot=snapshot)

    assert result is not None
    assert result.summary_message is None  # 仅裁剪，未生成摘要
    assert len(result.recent_history_records) > 0  # 仍有裁剪后的历史记录
    assert llm_adapter.last_completion_call is None


@pytest.mark.asyncio
async def test_summary_planner_proceeds_with_reasoning_content_in_old_history() -> None:
    """旧历史含 reasoning_content 时，压缩仍可进行；旧 thinking 被摘要替代，近期保持原样。"""
    agent = Agent(
        agent_id="agent-1",
        name="Agent",
        model="deepseek/deepseek-v4-flash",
        system_prompt="主系统提示词",
        temperature=0.2,
    )
    # 旧历史（轮次1）带 reasoning_content，最近两轮（轮次2-3）也带 reasoning_content。
    llm_adapter = FakeLLMAdapter(
        prompt_token_counts=[150, 150, 40],
        completion_text="压缩摘要",
    )
    planner = ContextSummaryPlanner(llm_adapter=llm_adapter, token_threshold=60)
    history = [
        _message("user", "第一轮问题"),
        _message("assistant", "第一轮回答", reasoning_content="旧历史思考"),
        _message("user", "第二轮问题"),
        _message("assistant", "第二轮回答", reasoning_content="近期思考1"),
        _message("user", "第三轮问题"),
    ]
    view = ContextHistoryViewBuilder.from_history(history=history, summary_state=None)
    snapshot = ContextBuilder.prepare_context_snapshot(
        system_message=StoredMessage.create(
            role="system", content="主系统提示词",
            timestamp=datetime(2026, 5, 6, 10, 0, 0, tzinfo=timezone.utc),
        ),
        extra_system_messages=None,
        history=view.active_history_messages,
        history_indices=view.active_history_indices,
        current_user_message=None,
    )

    result = await planner.plan(agent=agent, history_view=view, snapshot=snapshot)

    # 压缩应正常进行，不被 reasoning_content 阻塞。
    assert result is not None
    assert result.summary_message is not None
    # recent_history_records 中应包含最近两轮（轮次2-3）含 reasoning_content 的记录。
    assert len(result.recent_history_records) >= 1
