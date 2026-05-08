"""摘要持久化协作者测试。"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.core.models.stored_message import StoredMessage
from app.core.runtime.context_builder import SummaryPersistenceTarget
from app.core.runtime.context_summary_persistence import (
    SummaryPersistenceCoordinator,
    SummaryPersistencePlan,
)


def _summary_message(content: str) -> StoredMessage:
    """构造摘要消息。"""
    return StoredMessage.create(
        role="user",
        content=content,
        timestamp=datetime(2026, 5, 6, 10, 0, 0, tzinfo=timezone.utc),
        is_meta=True,
    )


@pytest.mark.asyncio
async def test_summary_persistence_coordinator_writes_main_summary_only() -> None:
    """主会话摘要计划只能写入主会话接口。"""
    session_store = AsyncMock()
    coordinator = SummaryPersistenceCoordinator(session_store=session_store)
    summary_message = _summary_message("<context_summary>主摘要</context_summary>")
    plan = SummaryPersistencePlan(
        target=SummaryPersistenceTarget.for_main("session-1"),
        summary_message=summary_message,
        active_start_message=None,
        active_start_offset=4,
    )

    await coordinator.persist(plan)

    session_store.append_main_context_summary.assert_awaited_once_with(
        session_id="session-1",
        message=summary_message,
        active_start_message=None,
        active_start_offset=4,
    )
    session_store.append_child_context_summary.assert_not_awaited()


@pytest.mark.asyncio
async def test_summary_persistence_coordinator_writes_child_summary_only() -> None:
    """child 摘要计划只能写入 child 会话接口。"""
    session_store = AsyncMock()
    coordinator = SummaryPersistenceCoordinator(session_store=session_store)
    summary_message = _summary_message("<context_summary>child 摘要</context_summary>")
    plan = SummaryPersistencePlan(
        target=SummaryPersistenceTarget.for_child("session-1", "writer-1"),
        summary_message=summary_message,
        active_start_message=None,
        active_start_offset=7,
    )

    await coordinator.persist(plan)

    session_store.append_child_context_summary.assert_awaited_once_with(
        session_id="session-1",
        child_id="writer-1",
        message=summary_message,
        active_start_message=None,
        active_start_offset=7,
    )
    session_store.append_main_context_summary.assert_not_awaited()


@pytest.mark.asyncio
async def test_summary_persistence_passes_active_start_message_when_not_none() -> None:
    """非空 active_start_message 应完整透传到存储层。"""
    session_store = AsyncMock()
    coordinator = SummaryPersistenceCoordinator(session_store=session_store)
    summary_message = _summary_message("<context_summary>摘要</context_summary>")
    active_msg = StoredMessage.create(
        role="user",
        content="活动窗口起点",
        timestamp=datetime(2026, 5, 6, 10, 0, 0, tzinfo=timezone.utc),
    )
    plan = SummaryPersistencePlan(
        target=SummaryPersistenceTarget.for_main("session-1"),
        summary_message=summary_message,
        active_start_message=active_msg,
        active_start_offset=4,
    )

    await coordinator.persist(plan)

    session_store.append_main_context_summary.assert_awaited_once_with(
        session_id="session-1",
        message=summary_message,
        active_start_message=active_msg,
        active_start_offset=4,
    )


@pytest.mark.asyncio
async def test_summary_persistence_raises_when_child_kind_missing_child_id() -> None:
    """kind='child' 但 child_id 为空时，必须直接报错而非兜底写空 key。"""
    session_store = AsyncMock()
    coordinator = SummaryPersistenceCoordinator(session_store=session_store)
    summary_message = _summary_message("<context_summary>child 摘要</context_summary>")
    plan = SummaryPersistencePlan(
        target=SummaryPersistenceTarget.for_child("session-1", child_id=""),  # 构造空 child_id
        summary_message=summary_message,
        active_start_message=None,
        active_start_offset=7,
    )

    with pytest.raises(ValueError, match="child_id 为空"):
        await coordinator.persist(plan)

    # 确认既没有主会话也没有 child 写入。
    session_store.append_main_context_summary.assert_not_awaited()
    session_store.append_child_context_summary.assert_not_awaited()


@pytest.mark.asyncio
async def test_summary_persistence_raises_when_kind_is_invalid() -> None:
    """非法 kind 值必须直接报错，不能静默路由。"""
    session_store = AsyncMock()
    coordinator = SummaryPersistenceCoordinator(session_store=session_store)
    summary_message = _summary_message("<context_summary>非法目标</context_summary>")
    # 绕过工厂方法直接设置非法 kind
    plan = SummaryPersistencePlan(
        target=SummaryPersistenceTarget(kind="parent", session_id="session-1"),
        summary_message=summary_message,
        active_start_message=None,
        active_start_offset=None,
    )

    with pytest.raises(ValueError, match="kind 非法"):
        await coordinator.persist(plan)
