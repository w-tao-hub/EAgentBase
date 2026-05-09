"""QueryToolResultTool 单元测试。"""

from __future__ import annotations

import pytest

from app.core.models.agent import Agent
from app.core.models.execution_context import ExecutionContext
from app.infra.store.redis_tool_result_store import RedisToolResultStore
from app.infra.tools.query_tool_result_tool import QueryToolResultTool


def _context(session_id: str = "session-1") -> ExecutionContext:
    """构造查询工具测试所需的最小执行上下文。"""
    return ExecutionContext(
        run_id="run-1",
        session_id=session_id,
        metadata={},
        agent=Agent(
            agent_id="agent-1",
            name="Query Tool Agent",
            model="gpt-4.1-mini",
            system_prompt="test",
            temperature=0.0,
        ),
    )


@pytest.mark.asyncio
async def test_query_tool_result_tool_returns_full_content_for_current_session(fake_redis) -> None:
    """测试 QueryToolResultTool 只能读当前会话内的完整结果。"""
    store = RedisToolResultStore(fake_redis, key_prefix="test")  # 创建真实 store，复用真实 key 格式与 Redis 行为。
    tool = QueryToolResultTool(store)  # 创建被测工具。
    key = await store.persist_result(
        session_id="session-1",
        tool_name="search",
        content="这是完整内容",
    )

    result = await tool.call({"key": key}, _context("session-1"))  # 使用当前 session 查询该 key。

    assert result.is_error is False  # 同 session 命中时应返回成功结果。
    assert result.content == "这是完整内容"  # 工具应返回完整正文而不是预览占位文本。


@pytest.mark.asyncio
async def test_query_tool_result_tool_rejects_foreign_session_key(fake_redis) -> None:
    """测试 QueryToolResultTool 会拒绝跨会话读取。"""
    store = RedisToolResultStore(fake_redis, key_prefix="test")  # 创建真实 store。
    tool = QueryToolResultTool(store)  # 创建被测工具。
    key = await store.persist_result(
        session_id="session-1",
        tool_name="search",
        content="这是完整内容",
    )

    result = await tool.call({"key": key}, _context("session-2"))  # 使用其他 session 查询同一 key。

    assert result.is_error is True  # 跨会话读取应失败。
    assert "不属于当前会话" in result.content  # 错误消息应明确指出权限边界。


@pytest.mark.asyncio
async def test_query_tool_result_tool_rejects_invalid_key_namespace(fake_redis) -> None:
    """测试 QueryToolResultTool 会拒绝非法 key 格式。"""
    store = RedisToolResultStore(fake_redis, key_prefix="test")  # 创建真实 store。
    tool = QueryToolResultTool(store)  # 创建被测工具。

    result = await tool.call({"key": "wrong:key"}, _context())  # 使用非法命名空间 key 查询。

    assert result.is_error is True  # 非法 key 应返回错误。
    assert "格式非法" in result.content  # 错误消息应明确指出 key 格式不合法。
