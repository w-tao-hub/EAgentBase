"""可恢复子代理查询工具测试。"""

from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from app.core.models.execution_context import ExecutionContext
from app.core.models.session import Session
from app.infra.store.redis_session_store import RedisSessionStore
from app.infra.tools.list_resumable_subagents_tool import ListResumableSubagentsTool
from tests.fakes import create_fake_agent


def _context(session_id: str = "session-1") -> ExecutionContext:
    """构造工具测试所需的最小执行上下文。"""
    return ExecutionContext(
        run_id="run-1",
        session_id=session_id,
        metadata={},
        agent=create_fake_agent(),
        run_type="master",
    )


@pytest.mark.asyncio
async def test_list_resumable_subagents_tool_returns_sorted_items(fake_redis):
    """测试工具会返回排序后的摘要列表。"""
    store = RedisSessionStore(fake_redis, key_prefix="test")
    await store.create_session(
        Session(
            session_id="session-1",
            agent_id="master-agent",
            created_at=datetime.now(timezone.utc),
        )
    )
    await store.upsert_session_child_summary("session-1", "child-b", "Plan", "第二个")
    await store.upsert_session_child_summary("session-1", "child-a", "Worker", "第一个")
    tool = ListResumableSubagentsTool(store)

    result = await tool.call({}, _context("session-1"))

    payload = json.loads(result.content)
    assert payload["items"] == [
        {"resume_id": "child-a", "subagent_type": "Worker", "description": "第一个"},
        {"resume_id": "child-b", "subagent_type": "Plan", "description": "第二个"},
    ]


@pytest.mark.asyncio
async def test_list_resumable_subagents_tool_returns_empty_list(fake_redis):
    """测试工具在无子代理时返回空列表。"""
    store = RedisSessionStore(fake_redis, key_prefix="test")
    await store.create_session(
        Session(
            session_id="session-1",
            agent_id="master-agent",
            created_at=datetime.now(timezone.utc),
        )
    )
    tool = ListResumableSubagentsTool(store)

    result = await tool.call({}, _context("session-1"))

    payload = json.loads(result.content)
    assert payload["items"] == []


@pytest.mark.asyncio
async def test_list_resumable_subagents_tool_filters_placeholder_summaries(fake_redis):
    """测试 subagent_type="" 的占位摘要不会出现在工具结果中。"""
    store = RedisSessionStore(fake_redis, key_prefix="test")
    await store.create_session(
        Session(
            session_id="session-filter",
            agent_id="master-agent",
            created_at=datetime.now(timezone.utc),
        )
    )
    # 用 ensure_session_child_registered 写入占位条目（subagent_type=""）
    await store.ensure_session_child_registered("session-filter", "placeholder-child")
    # 用 upsert_session_child_summary 写入正常条目
    await store.upsert_session_child_summary("session-filter", "normal-child", "Plan", "正常描述")

    tool = ListResumableSubagentsTool(store)

    result = await tool.call({}, _context("session-filter"))

    items = json.loads(result.content)["items"]
    # 只应包含正常条目，占位条目被过滤
    assert len(items) == 1
    assert items[0]["resume_id"] == "normal-child"
    assert items[0]["subagent_type"] == "Plan"


@pytest.mark.asyncio
async def test_list_resumable_subagents_tool_all_placeholders_returns_empty(fake_redis):
    """测试 session 下只有占位条目时工具返回空列表。"""
    store = RedisSessionStore(fake_redis, key_prefix="test")
    await store.create_session(
        Session(session_id="session-place", agent_id="master-agent", created_at=datetime.now(timezone.utc))
    )
    # 写入 2 个占位条目，不写入正常条目
    await store.ensure_session_child_registered("session-place", "placeholder-1")
    await store.ensure_session_child_registered("session-place", "placeholder-2")

    tool = ListResumableSubagentsTool(store)
    result = await tool.call({}, _context("session-place"))
    items = json.loads(result.content)["items"]
    assert items == []  # 全部被过滤


@pytest.mark.asyncio
async def test_list_resumable_subagents_tool_nonexistent_session_returns_empty(fake_redis):
    """测试不存在的 session_id 返回空列表（不报错）。"""
    store = RedisSessionStore(fake_redis, key_prefix="test")
    tool = ListResumableSubagentsTool(store)
    # 未创建 session 直接查询
    result = await tool.call({}, _context("session-nonexistent"))
    items = json.loads(result.content)["items"]
    assert items == []
