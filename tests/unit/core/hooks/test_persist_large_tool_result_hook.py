"""PersistLargeToolResultHook 单元测试。"""

from __future__ import annotations

import pytest

from app.core.hooks import (
    MAX_TOOL_RESULT_CHARACTERS,
    PersistLargeToolResultHook,
    QUERY_TOOL_RESULT_NAME,
    TOOL_RESULT_PREVIEW_CHARACTERS,
    ToolResponse,
)
from app.core.models.agent import Agent
from app.core.models.execution_context import ExecutionContext
from app.core.models.tool import ToolResult


class StubToolResultStore:
    """最小 store 替身。"""

    def __init__(self) -> None:
        """初始化替身状态。"""
        self.calls: list[dict[str, str]] = []  # 记录 persist_result 调用参数，便于断言。
        self.persisted_key = "agent:tool_result:stored-1"  # 固定返回 key，便于断言占位文本。

    async def persist_result(self, session_id: str, tool_name: str, content: str) -> str:
        """记录一次持久化调用，并返回固定 key。"""
        self.calls.append(
            {
                "session_id": session_id,
                "tool_name": tool_name,
                "content": content,
            }
        )
        return self.persisted_key  # 返回固定 key，便于精确断言占位文本。


def _context() -> ExecutionContext:
    """构造通用执行上下文。"""
    return ExecutionContext(
        run_id="run-1",
        session_id="session-1",
        metadata={},
        agent=Agent(
            agent_id="agent-1",
            name="Hook Test Agent",
            model="gpt-4.1-mini",
            system_prompt="test",
            temperature=0.0,
        ),
    )


@pytest.mark.asyncio
async def test_persist_large_tool_result_hook_keeps_small_result_unchanged() -> None:
    """测试未超阈值结果会原样透传。"""
    store = StubToolResultStore()  # 创建 store 替身，确认不会被调用。
    hook = PersistLargeToolResultHook(store)  # 创建被测 Hook。
    response = ToolResponse(
        tool_name="search",
        tool_call_id="call-1",
        result=ToolResult(content="small-result", is_error=False),
    )

    returned = await hook.after_tool(response, _context())  # 执行 Hook。

    assert returned is response  # 未超阈值时应直接透传原对象。
    assert store.calls == []  # 不应触发任何持久化写入。


@pytest.mark.asyncio
async def test_persist_large_tool_result_hook_persists_large_result_and_rewrites_placeholder() -> None:
    """测试超大结果会落盘并改写为占位文本。"""
    store = StubToolResultStore()  # 创建 store 替身。
    hook = PersistLargeToolResultHook(store)  # 创建被测 Hook。
    large_content = "A" * (MAX_TOOL_RESULT_CHARACTERS + 1)  # 构造刚好超过阈值的超大结果。
    response = ToolResponse(
        tool_name="search",
        tool_call_id="call-1",
        result=ToolResult(content=large_content, is_error=True),
    )

    returned = await hook.after_tool(response, _context())  # 执行 Hook，触发持久化替换。

    assert returned is not response  # 超大结果时应返回新响应对象，而不是原地修改。
    assert store.calls == [
        {
            "session_id": "session-1",
            "tool_name": "search",
            "content": large_content,
        }
    ]
    assert returned.result.is_error is True  # 错误标记应原样保留。
    assert store.persisted_key in returned.result.content  # 占位文本中应包含可查询 key。
    assert "<persisted-output>" in returned.result.content  # 占位文本应带规范包裹标签。
    assert "QueryToolResult" in returned.result.content  # 占位文本应提示使用查询工具。
    assert f"{'A' * TOOL_RESULT_PREVIEW_CHARACTERS}\n..." in returned.result.content  # 应只保留前 2000 字符预览。


@pytest.mark.asyncio
async def test_persist_large_tool_result_hook_skips_query_tool_result() -> None:
    """测试 query_tool_result 工具本身不会再次触发替换。"""
    store = StubToolResultStore()  # 创建 store 替身。
    hook = PersistLargeToolResultHook(store)  # 创建被测 Hook。
    response = ToolResponse(
        tool_name=QUERY_TOOL_RESULT_NAME,
        tool_call_id="call-1",
        result=ToolResult(content="B" * (MAX_TOOL_RESULT_CHARACTERS + 10), is_error=False),
    )

    returned = await hook.after_tool(response, _context())  # 执行 Hook。

    assert returned is response  # 查询工具自身结果应保持完整正文，不做替换。
    assert store.calls == []  # 查询工具结果不应写入持久化 store。
