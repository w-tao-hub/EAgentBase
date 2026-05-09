"""AgentLoop 的单元测试。

测试 AgentLoop 的多轮循环编排能力，包括：
- 纯文本响应场景（单轮终止）
- 单轮工具调用场景
- 多轮工具调用场景
- 多工具并行调用场景
- 工具错误场景
- max_turns 超限场景
- LLM 调用失败场景
"""

from __future__ import annotations

import asyncio
import json
import pytest
import time
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

from app.core.models.agent import Agent, AgentExecutionProfile, AgentPromptSource
from app.core.models.error import ErrorCode
from app.core.models.event import (
    Event,
    RunStartedEvent,
    MessageDeltaEvent,
    ToolUseStartedEvent,
    ToolUseCompletedEvent,
    RunCompletedEvent,
    RunFailedEvent,
)
from app.core.models.tool import Tool, ToolResult, ToolRegistry
from app.core.models.execution_context import ExecutionContext
from app.core.models.stored_message import StoredMessage
from app.core.hooks import (
    PersistLargeToolResultHook,
    ToolHook,
    ToolHookPipeline,
    ToolRequest,
    ToolResponse,
)
from app.core.loop.agent_loop import AgentLoop
from app.infra.store.redis_tool_result_store import RedisToolResultStore

# 类型导入
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from app.core.models.execution_context import ExecutionContext


class FakeAgentRuntime:
    """模拟 AgentRuntime，用于测试 AgentLoop。"""

    def __init__(
        self,
        chunks_list: list[list[dict]] | None = None,  # # 每轮返回的 chunks 列表
        raise_error: bool = False,  # # 是否抛出异常
    ) -> None:
        """初始化模拟运行时。"""
        self.chunks_list = chunks_list or [[]]  # # 保存每轮要返回的 chunks
        self.raise_error = raise_error  # # 是否抛出异常
        self.call_count = 0  # # 调用计数
        self.last_calls: list[dict] = []  # # 记录每次调用的参数

    async def stream_once(
        self,
        agent: Agent,
        messages: list[dict],
        tools: list[dict] | None = None,
        context: ExecutionContext | None = None,
    ) -> AsyncIterator[str | Any]:
        """模拟单次流式调用，返回 str 和 TurnComplete。"""
        from app.core.runtime.agent_runtime import TurnComplete, ToolCall, Function, UsageInfo

        # # 记录调用参数
        self.last_calls.append({
            "agent": agent,
            "messages": messages,
            "tools": tools,
            "context": context,
        })
        self.call_count += 1  # # 增加调用计数

        if self.raise_error:  # # 如果设置了抛出异常
            raise Exception("LLM 调用失败")  # # 抛出异常

        # # 获取当前轮次的 chunks
        chunks = self.chunks_list[min(self.call_count - 1, len(self.chunks_list) - 1)]

        # # 累积文本和工具调用
        full_text = ""
        tool_calls_list = []
        has_tool_calls = False

        for chunk in chunks:  # # 遍历 chunks
            if chunk.get("content"):  # # 如果有文本内容
                full_text += chunk["content"]
                yield chunk["content"]  # # yield str

            if chunk.get("tool_calls"):  # # 如果有工具调用
                has_tool_calls = True
                for tc in chunk["tool_calls"]:
                    index = tc.get("index", 0)
                    # # 累积工具调用信息
                    while len(tool_calls_list) <= index:
                        tool_calls_list.append({"id": "", "name": "", "arguments": "", "type": "function"})
                    if tc.get("id"):
                        tool_calls_list[index]["id"] = tc["id"]
                    if tc.get("function_name"):
                        tool_calls_list[index]["name"] = tc["function_name"]
                    if tc.get("arguments"):
                        tool_calls_list[index]["arguments"] += tc["arguments"]

        # # 转换工具调用格式
        final_tool_calls = None
        if tool_calls_list:
            final_tool_calls = [
                ToolCall(
                    id=tc["id"],
                    type=tc["type"],
                    function=Function(
                        name=tc["name"],
                        arguments=tc["arguments"],
                    ),
                )
                for tc in tool_calls_list if tc["name"]
            ]

        # # 构建 usage（简化处理）
        usage = None
        if chunks and chunks[-1].get("usage"):
            u = chunks[-1]["usage"]
            usage = UsageInfo(
                prompt_tokens=u.get("prompt_tokens", 0),
                completion_tokens=u.get("completion_tokens", 0),
                total_tokens=u.get("total_tokens", 0),
            )

        # # 返回最终结果
        yield TurnComplete(
            tool_calls=final_tool_calls,
            usage=usage,
        )


class FakeTool(Tool):
    """模拟工具，用于测试。"""

    def __init__(
        self,
        name: str,
        result: ToolResult | None = None,  # # 预设返回结果
        raise_error: bool = False,  # # 是否抛出异常
    ) -> None:
        """初始化模拟工具。"""
        self._name = name  # # 保存工具名称
        self._result = result or ToolResult(content=f"{name} 结果")  # # 保存预设结果
        self._raise_error = raise_error  # # 是否抛出异常
        self.last_input: dict | None = None  # # 记录最后一次调用输入

    @property
    def name(self) -> str:  # # type: ignore
        """工具名称。"""
        return self._name  # # 返回工具名称

    @property
    def description(self) -> str:  # # type: ignore
        """工具描述。"""
        return f"模拟工具: {self._name}"  # # 返回工具描述

    @property
    def input_schema(self) -> dict:  # # type: ignore
        """输入参数 schema。"""
        return {  # # 返回简单的 schema
            "type": "object",
            "properties": {
                "query": {"type": "string"},
            },
        }

    async def call(self, input: dict, context: "ExecutionContext") -> ToolResult:  # # type: ignore
        """执行工具。

        Args:
            input: 工具输入参数
            context: 执行上下文，包含 run_id、session_id、metadata、agent 等信息

        Returns:
            ToolResult: 工具执行结果
        """
        self.last_input = input  # # 记录输入参数
        if self._raise_error:  # # 如果设置了抛出异常
            raise Exception("工具执行失败")  # # 抛出异常
        return self._result  # # 返回预设结果


class PrefixToolInputHook(ToolHook):
    """为工具输入追加前缀的 Hook。"""

    def __init__(self, prefix: str) -> None:
        """初始化前缀值。"""
        super().__init__(fail_open=False)
        self._prefix = prefix

    async def before_tool(self, request: ToolRequest, context: ExecutionContext) -> ToolRequest:
        """改写工具输入，验证多个 Hook 串行生效。"""
        new_input = dict(request.tool_input)
        original_query = str(new_input.get("query", ""))
        new_input["query"] = f"{self._prefix}{original_query}"
        return ToolRequest(
            tool_name=request.tool_name,
            tool_call_id=request.tool_call_id,
            tool_input=new_input,
            tool=request.tool,
        )


class RewriteToolResultHook(ToolHook):
    """改写工具输出结果的 Hook。"""

    async def after_tool(self, response: ToolResponse, context: ExecutionContext) -> ToolResponse:
        """把工具结果改写成带后缀的文本。"""
        return ToolResponse(
            tool_name=response.tool_name,
            tool_call_id=response.tool_call_id,
            result=ToolResult(
                content=f"{response.result.content}-hooked",
                is_error=response.result.is_error,
            ),
        )


class FailClosedBeforeToolHook(ToolHook):
    """在 before_tool 阶段失败的 fail-closed Hook。"""

    def __init__(self) -> None:
        """声明该 Hook 失败时应中断当前工具调用。"""
        super().__init__(fail_open=False)

    async def before_tool(self, request: ToolRequest, context: ExecutionContext) -> ToolRequest:
        """直接抛出异常，验证错误收敛为工具错误结果。"""
        raise RuntimeError("tool hook boom")


# =============================================================================
# 辅助函数：构造 profile-only AgentLoop 测试用的执行配置
# =============================================================================


def build_test_profile(
    *,
    runtime,  # # AgentRuntime 实例
    tool_registry=None,  # # 工具注册表，默认空注册表
    max_turns=10,  # # 最大轮数
    tool_hook_pipeline=None,  # # 工具 Hook 管线，默认空管线
) -> AgentExecutionProfile:
    """构造 profile-only AgentLoop 测试使用的执行配置。

    统一创建一个标准的 AgentExecutionProfile，减少各测试中的样板代码。
    """
    agent = Agent(  # # 构造最小 Agent 配置
        agent_id="test-agent",
        name="Test Agent",
        model="gpt-4.1-mini",
        system_prompt="You are a helpful assistant.",
        temperature=0.2,
    )
    return AgentExecutionProfile(
        agent_id=agent.agent_id,
        agent=agent,
        prompt_source=AgentPromptSource(kind="file", path="test.md"),
        runtime=runtime,
        tool_registry=tool_registry or ToolRegistry(),
        tool_hook_pipeline=tool_hook_pipeline or ToolHookPipeline(),
        max_turns=max_turns,
    )


# =============================================================================
# 现有测试用例（适配为 profile-only 模式）
# =============================================================================


@pytest.mark.asyncio  # # 标记为异步测试
async def test_agent_loop_text_response_single_turn():
    """测试：纯文本响应场景（单轮终止）。

    验证当 LLM 返回纯文本且没有工具调用时，循环在单轮后终止。
    """
    # # 准备模拟数据：LLM 返回纯文本
    chunks = [  # # 构造 LLM 返回的 chunks
        {"content": "你好"},
        {"content": "，"},
        {"content": "世界"},
        {"finish_reason": "stop"},  # # 正常结束
    ]
    fake_runtime = FakeAgentRuntime(chunks_list=[chunks])  # # 创建模拟运行时

    # # 创建工具注册表（空）
    tool_registry = ToolRegistry()  # # 创建空注册表

    # # 创建 AgentLoop（无状态，只设默认最大轮数）
    agent_loop = AgentLoop()  # # 使用默认构造

    # # 构造执行配置
    profile = build_test_profile(runtime=fake_runtime, tool_registry=tool_registry, max_turns=10)  # # 构造 profile

    # # 准备测试数据
    messages = [{"role": "user", "content": "Say hello"}]  # # 构造用户消息

    # # 收集事件
    events = []  # # 初始化事件列表
    async for event in agent_loop.run(  # # 调用 AgentLoop（profile 模式）
        run_id="run-1",  # # 设置 run_id
        profile=profile,  # # 传入执行配置
        messages=messages,  # # 设置消息列表
    ):
        events.append(event)  # # 收集事件

    # # 验证事件序列
    assert len(events) == 5  # # 断言事件数量：started + 3 deltas + completed
    assert isinstance(events[0], RunStartedEvent)  # # 断言第一个事件是 started
    assert events[0].run_id == "run-1"  # # 断言 run_id 正确

    # # 验证 message_delta 事件
    for i, expected_content in enumerate(["你好", "，", "世界"], start=1):  # # 遍历预期的 chunks
        assert isinstance(events[i], MessageDeltaEvent)  # # 断言事件类型
        assert events[i].run_id == "run-1"  # # 断言 run_id 正确
        assert events[i].content == expected_content  # # 断言内容正确

    # # 验证 completed 事件
    assert isinstance(events[-1], RunCompletedEvent)  # # 断言最后一个事件是 completed
    assert events[-1].run_id == "run-1"  # # 断言 run_id 正确
    assert events[-1].output == "你好，世界"  # # 断言最终输出是拼接后的结果

    # # 验证只调用了一次 runtime
    assert fake_runtime.call_count == 1  # # 断言只调用了一次


@pytest.mark.asyncio  # # 标记为异步测试
async def test_agent_loop_single_tool_call():
    """测试：单轮工具调用场景。

    验证当 LLM 返回工具调用时，循环正确执行工具并返回结果。
    """
    # # 准备模拟工具
    fake_tool = FakeTool(name="search", result=ToolResult(content="搜索结果: Python"))  # # 创建模拟工具
    tool_registry = ToolRegistry()  # # 创建工具注册表
    tool_registry.register(fake_tool)  # # 注册工具

    # # 第一轮：LLM 返回工具调用
    chunks_round1 = [  # # 构造第一轮的 chunks
        {
            "content": None,
            "tool_calls": [  # # 工具调用（使用 dict 格式）
                {
                    "index": 0,
                    "id": "call_1",
                    "function_name": "search",
                    "arguments": '{"query": "Python"}',
                },
            ],
        },
        {"finish_reason": "tool_calls"},  # # 工具调用结束
    ]

    # # 第二轮：LLM 返回最终结果
    chunks_round2 = [  # # 构造第二轮的 chunks
        {"content": "根据搜索结果"},
        {"content": "，Python 是一门优秀的编程语言"},
        {"finish_reason": "stop"},  # # 正常结束
    ]

    fake_runtime = FakeAgentRuntime(chunks_list=[chunks_round1, chunks_round2])  # # 创建模拟运行时

    # # 创建 AgentLoop 和执行配置
    agent_loop = AgentLoop()  # # 使用默认构造
    profile = build_test_profile(runtime=fake_runtime, tool_registry=tool_registry, max_turns=10)  # # 构造 profile

    # # 准备测试数据
    messages = [{"role": "user", "content": "搜索 Python"}]  # # 构造用户消息

    # # 收集事件
    events = []  # # 初始化事件列表
    async for event in agent_loop.run(  # # 调用 AgentLoop（profile 模式）
        run_id="run-1",
        profile=profile,
        messages=messages,
    ):
        events.append(event)  # # 收集事件

    # # 验证事件序列
    assert isinstance(events[0], RunStartedEvent)  # # 断言第一个事件是 started

    # # 验证工具相关事件
    tool_started_found = False  # # 标记是否找到 tool_started 事件
    tool_completed_found = False  # # 标记是否找到 tool_completed 事件
    for event in events:  # # 遍历事件
        if isinstance(event, ToolUseStartedEvent):  # # 如果是工具开始事件
            tool_started_found = True  # # 设置标记
            assert event.run_id == "run-1"  # # 断言 run_id 正确
            assert event.tool_name == "search"  # # 断言工具名称正确
            assert event.tool_call_id == "call_1"  # # 断言工具调用 ID 正确
        elif isinstance(event, ToolUseCompletedEvent):  # # 如果是工具完成事件
            tool_completed_found = True  # # 设置标记
            assert event.run_id == "run-1"  # # 断言 run_id 正确
            assert event.tool_name == "search"  # # 断言工具名称正确
            assert event.result == "搜索结果: Python"  # # 断言结果正确
            assert event.is_error is False  # # 断言不是错误

    assert tool_started_found, "应该发出 ToolUseStartedEvent"  # # 断言找到工具开始事件
    assert tool_completed_found, "应该发出 ToolUseCompletedEvent"  # # 断言找到工具完成事件

    # # 验证调用了两次 runtime
    assert fake_runtime.call_count == 2  # # 断言调用了两次

    # # 验证第二轮的消息包含工具结果
    second_call_messages = fake_runtime.last_calls[1]["messages"]  # # 获取第二轮的消息
    tool_result_message = second_call_messages[-1]  # # 获取最后一条消息（工具结果）
    assert tool_result_message["role"] == "tool"  # # 断言角色是 tool
    assert tool_result_message["content"] == "搜索结果: Python"  # # 断言内容是工具结果

    # # 验证 completed 事件
    assert isinstance(events[-1], RunCompletedEvent)  # # 断言最后一个事件是 completed
    assert "Python 是一门优秀的编程语言" in events[-1].output  # # 断言输出包含预期内容


@pytest.mark.asyncio  # # 标记为异步测试
async def test_agent_loop_appends_tool_session_message_into_next_round_messages():
    """测试工具返回的 StoredMessage 会进入下一轮发送给模型的消息列表。"""
    fake_tool = FakeTool(
        name="skill",
        result=ToolResult(
            content="技能加载完成: demo",
            stored_message=StoredMessage.create(
                role="user",
                content="<skill_name>demo</skill_name><skill_message>full doc</skill_message>",
                timestamp=datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc),
                is_meta=True,
            ),
        ),
    )
    tool_registry = ToolRegistry()
    tool_registry.register(fake_tool)

    chunks_round1 = [
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_skill",
                    "function_name": "skill",
                    "arguments": '{"skill": "demo"}',
                },
            ],
        },
        {"finish_reason": "tool_calls"},
    ]
    chunks_round2 = [
        {"content": "已读取技能"},
        {"finish_reason": "stop"},
    ]
    fake_runtime = FakeAgentRuntime(chunks_list=[chunks_round1, chunks_round2])

    # # 创建 AgentLoop 和执行配置
    agent_loop = AgentLoop()  # # 使用默认构造
    profile = build_test_profile(runtime=fake_runtime, tool_registry=tool_registry, max_turns=10)  # # 构造 profile

    messages = [{"role": "user", "content": "加载 demo 技能"}]

    events = []
    async for event in agent_loop.run(  # # 调用 AgentLoop（profile 模式）
        run_id="run-skill",
        profile=profile,
        messages=messages,
    ):
        events.append(event)

    completed_event = next(event for event in events if isinstance(event, ToolUseCompletedEvent))
    assert completed_event.stored_message is not None
    assert completed_event.stored_message.is_meta is True

    second_call_messages = fake_runtime.last_calls[1]["messages"]
    assert second_call_messages[-1]["role"] == "user"
    assert second_call_messages[-1]["content"] == (
        "<skill_name>demo</skill_name><skill_message>full doc</skill_message>"
    )


@pytest.mark.asyncio  # # 标记为异步测试
async def test_agent_loop_multi_turn_tool_calls():
    """测试：多轮工具调用场景。

    验证当需要多轮工具调用时，循环正确处理每一轮。
    """
    # # 准备模拟工具
    search_tool = FakeTool(name="search", result=ToolResult(content="搜索结果"))  # # 创建搜索工具
    calc_tool = FakeTool(name="calculate", result=ToolResult(content="42"))  # # 创建计算工具
    tool_registry = ToolRegistry()  # # 创建工具注册表
    tool_registry.register(search_tool)  # # 注册搜索工具
    tool_registry.register(calc_tool)  # # 注册计算工具

    # # 第一轮：调用 search
    chunks_round1 = [  # # 构造第一轮的 chunks
        {
            "content": None,
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_1",
                    "function_name": "search",
                    "arguments": '{"query": "test"}',
                },
            ],
        },
        {"finish_reason": "tool_calls"},
    ]

    # # 第二轮：调用 calculate
    chunks_round2 = [  # # 构造第二轮的 chunks
        {
            "content": None,
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_2",
                    "function_name": "calculate",
                    "arguments": '{"expr": "6*7"}',
                },
            ],
        },
        {"finish_reason": "tool_calls"},
    ]

    # # 第三轮：返回最终结果
    chunks_round3 = [  # # 构造第三轮的 chunks
        {"content": "完成"},
        {"finish_reason": "stop"},
    ]

    fake_runtime = FakeAgentRuntime(chunks_list=[chunks_round1, chunks_round2, chunks_round3])  # # 创建模拟运行时

    # # 创建 AgentLoop 和执行配置
    agent_loop = AgentLoop()  # # 使用默认构造
    profile = build_test_profile(runtime=fake_runtime, tool_registry=tool_registry, max_turns=10)  # # 构造 profile

    # # 准备测试数据
    messages = [{"role": "user", "content": "执行任务"}]

    # # 收集事件
    events = []  # # 初始化事件列表
    async for event in agent_loop.run(run_id="run-1", profile=profile, messages=messages):
        events.append(event)  # # 收集事件

    # # 验证调用了三次 runtime
    assert fake_runtime.call_count == 3  # # 断言调用了三次

    # # 验证有两个工具调用事件对
    tool_started_count = sum(1 for e in events if isinstance(e, ToolUseStartedEvent))  # # 统计工具开始事件
    tool_completed_count = sum(1 for e in events if isinstance(e, ToolUseCompletedEvent))  # # 统计工具完成事件
    assert tool_started_count == 2  # # 断言有两个工具开始事件
    assert tool_completed_count == 2  # # 断言有两个工具完成事件

    # # 验证 completed 事件
    assert isinstance(events[-1], RunCompletedEvent)  # # 断言最后一个事件是 completed


@pytest.mark.asyncio  # # 标记为异步测试
async def test_agent_loop_tool_not_found():
    """测试：工具未找到场景。

    验证当 LLM 请求未注册的工具时，返回错误结果。
    """
    # # 创建空工具注册表
    tool_registry = ToolRegistry()  # # 创建空注册表

    # # 第一轮：LLM 返回未注册的工具调用
    chunks_round1 = [  # # 构造第一轮的 chunks
        {
            "content": None,
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_1",
                    "function_name": "unknown_tool",  # # 未注册的工具
                    "arguments": '{"query": "test"}',
                },
            ],
        },
        {"finish_reason": "tool_calls"},
    ]

    # # 第二轮：LLM 返回最终结果
    chunks_round2 = [  # # 构造第二轮的 chunks
        {"content": "收到"},
        {"finish_reason": "stop"},
    ]

    fake_runtime = FakeAgentRuntime(chunks_list=[chunks_round1, chunks_round2])  # # 创建模拟运行时

    # # 创建 AgentLoop 和执行配置
    agent_loop = AgentLoop()  # # 使用默认构造
    profile = build_test_profile(runtime=fake_runtime, tool_registry=tool_registry, max_turns=10)  # # 构造 profile

    # # 准备测试数据
    messages = [{"role": "user", "content": "测试"}]

    # # 收集事件
    events = []  # # 初始化事件列表
    async for event in agent_loop.run(run_id="run-1", profile=profile, messages=messages):
        events.append(event)  # # 收集事件

    # # 验证工具完成事件包含错误
    tool_completed_event = None  # # 初始化变量
    for event in events:  # # 遍历事件
        if isinstance(event, ToolUseCompletedEvent):  # # 如果是工具完成事件
            tool_completed_event = event  # # 保存事件
            break  # # 跳出循环

    assert tool_completed_event is not None  # # 断言找到工具完成事件
    assert tool_completed_event.is_error is True  # # 断言是错误结果
    assert "未知工具" in tool_completed_event.result  # # 断言错误消息包含"未知工具"
    assert "unknown_tool" in tool_completed_event.result  # # 断言错误消息包含工具名

    # # 验证 completed 事件
    assert isinstance(events[-1], RunCompletedEvent)  # # 断言最后一个事件是 completed


@pytest.mark.asyncio  # # 标记为异步测试
async def test_agent_loop_tool_execution_error():
    """测试：工具执行异常场景。

    验证当工具执行抛出异常时，返回错误结果。
    """
    # # 准备会抛出异常的模拟工具
    error_tool = FakeTool(
        name="error_tool",
        result=ToolResult(content="", is_error=True),
        raise_error=True,  # # 设置抛出异常
    )
    tool_registry = ToolRegistry()  # # 创建工具注册表
    tool_registry.register(error_tool)  # # 注册工具

    # # 第一轮：LLM 返回工具调用
    chunks_round1 = [  # # 构造第一轮的 chunks
        {
            "content": None,
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_1",
                    "function_name": "error_tool",
                    "arguments": '{}',
                },
            ],
        },
        {"finish_reason": "tool_calls"},
    ]

    # # 第二轮：LLM 返回最终结果
    chunks_round2 = [  # # 构造第二轮的 chunks
        {"content": "处理完成"},
        {"finish_reason": "stop"},
    ]

    fake_runtime = FakeAgentRuntime(chunks_list=[chunks_round1, chunks_round2])  # # 创建模拟运行时

    # # 创建 AgentLoop 和执行配置
    agent_loop = AgentLoop()  # # 使用默认构造
    profile = build_test_profile(runtime=fake_runtime, tool_registry=tool_registry, max_turns=10)  # # 构造 profile

    # # 准备测试数据
    messages = [{"role": "user", "content": "测试"}]

    # # 收集事件
    events = []  # # 初始化事件列表
    async for event in agent_loop.run(run_id="run-1", profile=profile, messages=messages):
        events.append(event)  # # 收集事件

    # # 验证工具完成事件包含错误
    tool_completed_event = None  # # 初始化变量
    for event in events:  # # 遍历事件
        if isinstance(event, ToolUseCompletedEvent):  # # 如果是工具完成事件
            tool_completed_event = event  # # 保存事件
            break  # # 跳出循环

    assert tool_completed_event is not None  # # 断言找到工具完成事件
    assert tool_completed_event.is_error is True  # # 断言是错误结果
    assert "工具执行失败" in tool_completed_event.result  # # 断言错误消息包含预期内容

    # # 验证 completed 事件
    assert isinstance(events[-1], RunCompletedEvent)  # # 断言最后一个事件是 completed


@pytest.mark.asyncio  # # 标记为异步测试
async def test_agent_loop_tool_arguments_json_parse_error():
    """测试：工具参数 JSON 解析失败场景。

    验证当工具参数 JSON 解析失败时，返回错误结果。
    """
    # # 准备模拟工具
    fake_tool = FakeTool(name="search", result=ToolResult(content="结果"))  # # 创建模拟工具
    tool_registry = ToolRegistry()  # # 创建工具注册表
    tool_registry.register(fake_tool)  # # 注册工具

    # # 第一轮：LLM 返回工具调用（参数是非法 JSON）
    chunks_round1 = [  # # 构造第一轮的 chunks
        {
            "content": None,
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_1",
                    "function_name": "search",
                    "arguments": '{invalid json}',  # # 非法 JSON
                },
            ],
        },
        {"finish_reason": "tool_calls"},
    ]

    # # 第二轮：LLM 返回最终结果
    chunks_round2 = [  # # 构造第二轮的 chunks
        {"content": "完成"},
        {"finish_reason": "stop"},
    ]

    fake_runtime = FakeAgentRuntime(chunks_list=[chunks_round1, chunks_round2])  # # 创建模拟运行时

    # # 创建 AgentLoop 和执行配置
    agent_loop = AgentLoop()  # # 使用默认构造
    profile = build_test_profile(runtime=fake_runtime, tool_registry=tool_registry, max_turns=10)  # # 构造 profile

    # # 准备测试数据
    messages = [{"role": "user", "content": "测试"}]

    # # 收集事件
    events = []  # # 初始化事件列表
    async for event in agent_loop.run(run_id="run-1", profile=profile, messages=messages):
        events.append(event)  # # 收集事件

    # # 验证工具完成事件包含错误
    tool_completed_event = None  # # 初始化变量
    for event in events:  # # 遍历事件
        if isinstance(event, ToolUseCompletedEvent):  # # 如果是工具完成事件
            tool_completed_event = event  # # 保存事件
            break  # # 跳出循环

    assert tool_completed_event is not None  # # 断言找到工具完成事件
    assert tool_completed_event.is_error is True  # # 断言是错误结果
    assert "JSON 解析失败" in tool_completed_event.result  # # 断言错误消息包含预期内容

    # # 验证 completed 事件
    assert isinstance(events[-1], RunCompletedEvent)  # # 断言最后一个事件是 completed


@pytest.mark.asyncio  # # 标记为异步测试
async def test_agent_loop_tool_hooks_rewrite_started_event_input_and_completed_result():
    """测试：工具 Hook 会串行改写 started 事件入参与 completed 结果。"""
    fake_tool = FakeTool(name="search", result=ToolResult(content="原始结果"))
    tool_registry = ToolRegistry()
    tool_registry.register(fake_tool)

    chunks_round1 = [
        {
            "content": None,
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_1",
                    "function_name": "search",
                    "arguments": '{"query": "test"}',
                },
            ],
        },
        {"finish_reason": "tool_calls"},
    ]
    chunks_round2 = [
        {"content": "完成"},
        {"finish_reason": "stop"},
    ]
    fake_runtime = FakeAgentRuntime(chunks_list=[chunks_round1, chunks_round2])

    # # 创建 AgentLoop 和执行配置，Hook 管线通过 profile 传入
    agent_loop = AgentLoop()  # # 使用默认构造
    profile = build_test_profile(
        runtime=fake_runtime,
        tool_registry=tool_registry,
        max_turns=10,
        tool_hook_pipeline=ToolHookPipeline(
            hooks=[
                PrefixToolInputHook(prefix="first-"),
                PrefixToolInputHook(prefix="second-"),
                RewriteToolResultHook(),
            ]
        ),
    )

    events = []
    async for event in agent_loop.run(run_id="run-1", profile=profile, messages=[{"role": "user", "content": "测试"}]):
        events.append(event)

    started_event = next(event for event in events if isinstance(event, ToolUseStartedEvent))
    completed_event = next(event for event in events if isinstance(event, ToolUseCompletedEvent))

    assert started_event.tool_input == {"query": "second-first-test"}
    assert fake_tool.last_input == {"query": "second-first-test"}
    assert completed_event.result == "原始结果-hooked"
    assert completed_event.is_error is False
    assert fake_runtime.last_calls[1]["messages"][-1]["content"] == "原始结果-hooked"


@pytest.mark.asyncio  # # 标记为异步测试
async def test_agent_loop_persists_large_tool_result_and_reuses_placeholder_in_next_turn(fake_redis):
    """测试：超大工具结果会写入 Redis，并在 completed 事件与下一轮上下文中统一替换成占位文本。"""
    large_content = "超大输出" * 4000  # # 构造超过 15000 字符阈值的工具输出。
    fake_tool = FakeTool(name="search", result=ToolResult(content=large_content))  # # 创建返回超大结果的工具。
    tool_registry = ToolRegistry()  # # 创建工具注册表。
    tool_registry.register(fake_tool)  # # 注册测试工具。

    chunks_round1 = [  # # 第一轮仅返回一次 search 工具调用。
        {
            "content": None,
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_1",
                    "function_name": "search",
                    "arguments": '{"query": "large"}',
                },
            ],
        },
        {"finish_reason": "tool_calls"},
    ]
    chunks_round2 = [  # # 第二轮返回普通文本完成整个回合。
        {"content": "完成"},
        {"finish_reason": "stop"},
    ]
    fake_runtime = FakeAgentRuntime(chunks_list=[chunks_round1, chunks_round2])  # # 创建两轮运行时替身。
    tool_result_store = RedisToolResultStore(fake_redis, key_prefix="test")  # # 创建真实 Redis 大结果存储。

    # # 创建 AgentLoop 和执行配置，Hook 管线通过 profile 传入
    agent_loop = AgentLoop()  # # 使用默认构造
    profile = build_test_profile(
        runtime=fake_runtime,
        tool_registry=tool_registry,
        max_turns=10,
        tool_hook_pipeline=ToolHookPipeline(
            hooks=[PersistLargeToolResultHook(tool_result_store)]
        ),
    )

    events = []  # # 收集整条执行链路的全部事件。
    async for event in agent_loop.run(run_id="run-1", profile=profile, messages=[{"role": "user", "content": "测试"}], session_id="session-1"):
        events.append(event)

    completed_event = next(event for event in events if isinstance(event, ToolUseCompletedEvent))  # # 读取工具完成事件。
    assert completed_event.is_error is False  # # 正常工具结果即使被替换预览，也应保持成功态。
    assert "<persisted-output>" in completed_event.result  # # SSE 可见结果应为占位文本。
    assert "QueryToolResult" in completed_event.result  # # 占位文本应包含完整结果查询提示。

    persisted_key = completed_event.result.split("（", 1)[1].split("）", 1)[0]  # # 从占位文本里解析出持久化 key。
    stored_result = await tool_result_store.get_result(key=persisted_key, session_id="session-1")  # # 从 Redis 取回完整正文。
    assert stored_result is not None  # # 完整结果应已成功落 Redis。
    assert stored_result.content == large_content  # # Redis 中应保留完整原文。

    second_call_messages = fake_runtime.last_calls[1]["messages"]  # # 读取第二轮模型实际看到的上下文消息。
    tool_message = second_call_messages[-1]  # # 最后一条消息应是上一轮工具结果。
    assert tool_message["role"] == "tool"  # # 工具结果应继续以 tool 消息角色进入下一轮上下文。
    assert tool_message["content"] == completed_event.result  # # 下一轮上下文看到的也应是同一份占位文本。


@pytest.mark.asyncio  # # 标记为异步测试
async def test_agent_loop_fail_closed_tool_hook_converts_current_tool_call_to_error_result():
    """测试：fail-closed 的工具 Hook 失败时，只把当前工具调用收敛成错误结果。"""
    fake_tool = FakeTool(name="search", result=ToolResult(content="原始结果"))
    tool_registry = ToolRegistry()
    tool_registry.register(fake_tool)

    chunks_round1 = [
        {
            "content": None,
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_1",
                    "function_name": "search",
                    "arguments": '{"query": "test"}',
                },
            ],
        },
        {"finish_reason": "tool_calls"},
    ]
    chunks_round2 = [
        {"content": "结束"},
        {"finish_reason": "stop"},
    ]
    fake_runtime = FakeAgentRuntime(chunks_list=[chunks_round1, chunks_round2])

    # # 创建 AgentLoop 和执行配置，Hook 管线通过 profile 传入
    agent_loop = AgentLoop()  # # 使用默认构造
    profile = build_test_profile(
        runtime=fake_runtime,
        tool_registry=tool_registry,
        max_turns=10,
        tool_hook_pipeline=ToolHookPipeline(hooks=[FailClosedBeforeToolHook()]),
    )

    events = []
    async for event in agent_loop.run(run_id="run-1", profile=profile, messages=[{"role": "user", "content": "测试"}]):
        events.append(event)

    completed_event = next(event for event in events if isinstance(event, ToolUseCompletedEvent))

    assert fake_tool.last_input is None
    assert completed_event.is_error is True
    assert "tool hook boom" in completed_event.result
    assert isinstance(events[-1], RunCompletedEvent)


@pytest.mark.asyncio  # # 标记为异步测试
async def test_agent_loop_max_turns_exceeded():
    """测试：max_turns 超限场景。

    验证当循环超过最大轮数时，返回 RunFailedEvent。
    """
    # # 准备模拟数据：LLM 总是返回工具调用（不会自然结束）
    chunks = [  # # 构造 LLM 返回的 chunks
        {
            "content": None,
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_1",
                    "function_name": "search",
                    "arguments": '{"query": "test"}',
                },
            ],
        },
        {"finish_reason": "tool_calls"},
    ]
    fake_runtime = FakeAgentRuntime(chunks_list=[chunks])  # # 创建模拟运行时

    # # 准备模拟工具
    fake_tool = FakeTool(name="search", result=ToolResult(content="结果"))  # # 创建模拟工具
    tool_registry = ToolRegistry()  # # 创建工具注册表
    tool_registry.register(fake_tool)  # # 注册工具

    # # 创建 AgentLoop 和执行配置，设置较小的 max_turns
    agent_loop = AgentLoop(default_max_turns=3)  # # 设置默认最大轮数为 3
    profile = build_test_profile(runtime=fake_runtime, tool_registry=tool_registry, max_turns=3)  # # profile 中 max_turns=3

    # # 准备测试数据
    messages = [{"role": "user", "content": "测试"}]

    # # 收集事件
    events = []  # # 初始化事件列表
    async for event in agent_loop.run(run_id="run-1", profile=profile, messages=messages):
        events.append(event)  # # 收集事件

    # # 验证事件序列
    assert isinstance(events[0], RunStartedEvent)  # # 断言第一个事件是 started

    # # 验证最后一个事件是 RunFailedEvent
    assert isinstance(events[-1], RunFailedEvent)  # # 断言最后一个事件是 failed
    assert events[-1].run_id == "run-1"  # # 断言 run_id 正确
    assert "max_turns" in events[-1].message.lower() or "轮数" in events[-1].message  # # 断言错误消息包含相关信息

    # # 验证调用了 max_turns 次 runtime
    assert fake_runtime.call_count == 3  # # 断言调用了 3 次


@pytest.mark.asyncio  # # 标记为异步测试
async def test_agent_loop_llm_call_failure():
    """测试：LLM 调用失败场景。

    验证当 LLM 调用抛出异常时，返回 RunFailedEvent。
    """
    # # 准备会抛出异常的模拟运行时
    fake_runtime = FakeAgentRuntime(raise_error=True)  # # 创建会抛出异常的模拟运行时

    # # 创建工具注册表（空）
    tool_registry = ToolRegistry()  # # 创建空注册表

    # # 创建 AgentLoop 和执行配置
    agent_loop = AgentLoop()  # # 使用默认构造
    profile = build_test_profile(runtime=fake_runtime, tool_registry=tool_registry, max_turns=10)  # # 构造 profile

    # # 准备测试数据
    messages = [{"role": "user", "content": "测试"}]

    # # 收集事件（不应抛出异常）
    events = []  # # 初始化事件列表
    try:  # # 尝试捕获可能的异常
        async for event in agent_loop.run(run_id="run-1", profile=profile, messages=messages):
            events.append(event)  # # 收集事件
    except Exception as e:  # # 捕获异常
        pytest.fail(f"AgentLoop 不应向外抛出异常: {e}")  # # 如果抛出异常则测试失败

    # # 验证事件序列
    assert len(events) == 2  # # 断言事件数量：started + failed
    assert isinstance(events[0], RunStartedEvent)  # # 断言第一个事件是 started
    assert isinstance(events[-1], RunFailedEvent)  # # 断言最后一个事件是 failed
    assert events[-1].run_id == "run-1"  # # 断言 run_id 正确
    assert "LLM" in events[-1].message or "失败" in events[-1].message  # # 断言错误消息包含相关信息


# =============================================================================
# 并行工具调用测试
# =============================================================================


class DelayedFakeTool(Tool):
    """带延迟的模拟工具，用于测试并行执行。

    该工具在执行时会延迟指定时间，并记录实际执行时间，
    用于验证多个工具是否真正并行执行。
    """

    def __init__(
        self,
        name: str,
        delay: float,  # # 延迟时间（秒）
        result: ToolResult | None = None,
    ) -> None:
        """初始化带延迟的模拟工具。"""
        self._name = name
        self._delay = delay
        self._result = result or ToolResult(content=f"{name} 结果")
        self.execution_time: float = 0.0  # # 实际执行时间
        self.last_input: dict | None = None

    @property
    def name(self) -> str:  # # type: ignore
        """工具名称。"""
        return self._name

    @property
    def description(self) -> str:  # # type: ignore
        """工具描述。"""
        return f"带延迟的模拟工具: {self._name}"

    @property
    def input_schema(self) -> dict:  # # type: ignore
        """输入参数 schema。"""
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
            },
        }

    async def call(self, input: dict, context: "ExecutionContext") -> ToolResult:  # # type: ignore
        """执行工具，带延迟。"""
        self.last_input = input
        start_time = time.monotonic()
        await asyncio.sleep(self._delay)  # # 异步延迟
        self.execution_time = time.monotonic() - start_time
        return self._result


@pytest.mark.asyncio  # # 标记为异步测试
async def test_agent_loop_parallel_tool_calls():
    """测试：同一轮次多个工具并行调用。

    验证当 LLM 返回多个工具调用时：
    1. 所有 ToolUseStartedEvent 按顺序首先发出
    2. 工具并行执行
    3. 所有 ToolUseCompletedEvent 按原顺序发出
    4. 结果按原顺序添加到 conversation_messages
    """
    # # 准备带延迟的模拟工具
    tool1 = DelayedFakeTool(name="tool1", delay=0.1, result=ToolResult(content="结果1"))
    tool2 = DelayedFakeTool(name="tool2", delay=0.05, result=ToolResult(content="结果2"))

    tool_registry = ToolRegistry()
    tool_registry.register(tool1)
    tool_registry.register(tool2)

    # # 第一轮：LLM 返回两个工具调用
    chunks_round1 = [
        {
            "content": None,
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_1",
                    "function_name": "tool1",
                    "arguments": '{"query": "test1"}',
                },
                {
                    "index": 1,
                    "id": "call_2",
                    "function_name": "tool2",
                    "arguments": '{"query": "test2"}',
                },
            ],
        },
        {"finish_reason": "tool_calls"},
    ]

    # # 第二轮：LLM 返回最终结果
    chunks_round2 = [
        {"content": "完成"},
        {"finish_reason": "stop"},
    ]

    fake_runtime = FakeAgentRuntime(chunks_list=[chunks_round1, chunks_round2])

    # # 创建 AgentLoop 和执行配置
    agent_loop = AgentLoop()  # # 使用默认构造
    profile = build_test_profile(runtime=fake_runtime, tool_registry=tool_registry, max_turns=10)  # # 构造 profile

    # # 准备测试数据
    messages = [{"role": "user", "content": "测试"}]

    # # 收集事件
    events = []
    async for event in agent_loop.run(run_id="run-1", profile=profile, messages=messages):
        events.append(event)

    # # 验证事件顺序：所有 started 事件必须在任何 completed 事件之前
    started_indices = []
    completed_indices = []
    for i, event in enumerate(events):
        if isinstance(event, ToolUseStartedEvent):
            started_indices.append((i, event.tool_call_id))
        elif isinstance(event, ToolUseCompletedEvent):
            completed_indices.append((i, event.tool_call_id))

    # # 验证所有 started 事件在任何 completed 事件之前
    max_started_idx = max(idx for idx, _ in started_indices)
    min_completed_idx = min(idx for idx, _ in completed_indices)
    assert max_started_idx < min_completed_idx, "所有 started 事件必须在 completed 事件之前"

    # # 验证 started 事件顺序与 tool_calls 一致
    assert started_indices[0][1] == "call_1"
    assert started_indices[1][1] == "call_2"

    # # 验证 completed 事件顺序与 tool_calls 一致
    assert completed_indices[0][1] == "call_1"
    assert completed_indices[1][1] == "call_2"

    # # 验证工具确实是并行执行的
    # 如果串行执行，总时间约为 0.15s；并行执行约为 0.1s
    # tool2 延迟更短（0.05s），tool1 延迟更长（0.1s）
    # 并行执行时，两者几乎同时开始，tool2 应该比 tool1 先完成（实际执行时间更短）
    assert tool2.execution_time < tool1.execution_time + 0.02, "工具应并行执行"

    # # 验证 completed 事件结果正确
    completed_events = [e for e in events if isinstance(e, ToolUseCompletedEvent)]
    assert completed_events[0].result == "结果1"
    assert completed_events[1].result == "结果2"

    # # 验证对话消息顺序正确
    second_call_messages = fake_runtime.last_calls[1]["messages"]
    tool_messages = [m for m in second_call_messages if m["role"] == "tool"]
    assert len(tool_messages) == 2
    assert tool_messages[0]["tool_call_id"] == "call_1"
    assert tool_messages[1]["tool_call_id"] == "call_2"


@pytest.mark.asyncio  # # 标记为异步测试
async def test_agent_loop_parallel_tool_calls_partial_failure():
    """测试：并行执行时部分工具失败。

    验证当多个工具并行执行时，单个工具失败不影响其他工具，
    且所有结果按原顺序返回。
    """
    # # 准备工具：一个成功，一个失败
    success_tool = FakeTool(name="success_tool", result=ToolResult(content="成功结果"))
    error_tool = FakeTool(
        name="error_tool",
        result=ToolResult(content="错误结果", is_error=True),
        raise_error=True,
    )

    tool_registry = ToolRegistry()
    tool_registry.register(success_tool)
    tool_registry.register(error_tool)

    # # 第一轮：LLM 返回两个工具调用（第一个失败，第二个成功）
    chunks_round1 = [
        {
            "content": None,
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_1",
                    "function_name": "error_tool",
                    "arguments": '{}',
                },
                {
                    "index": 1,
                    "id": "call_2",
                    "function_name": "success_tool",
                    "arguments": '{}',
                },
            ],
        },
        {"finish_reason": "tool_calls"},
    ]

    # # 第二轮：LLM 返回最终结果
    chunks_round2 = [
        {"content": "处理完成"},
        {"finish_reason": "stop"},
    ]

    fake_runtime = FakeAgentRuntime(chunks_list=[chunks_round1, chunks_round2])

    # # 创建 AgentLoop 和执行配置
    agent_loop = AgentLoop()  # # 使用默认构造
    profile = build_test_profile(runtime=fake_runtime, tool_registry=tool_registry, max_turns=10)  # # 构造 profile

    # # 准备测试数据
    messages = [{"role": "user", "content": "测试"}]

    # # 收集事件
    events = []
    async for event in agent_loop.run(run_id="run-1", profile=profile, messages=messages):
        events.append(event)

    # # 验证两个工具都有 started 和 completed 事件
    started_events = [e for e in events if isinstance(e, ToolUseStartedEvent)]
    completed_events = [e for e in events if isinstance(e, ToolUseCompletedEvent)]

    assert len(started_events) == 2
    assert len(completed_events) == 2

    # # 验证错误状态：第一个失败，第二个成功
    error_completed = [e for e in completed_events if e.tool_call_id == "call_1"][0]
    success_completed = [e for e in completed_events if e.tool_call_id == "call_2"][0]

    assert error_completed.is_error is True
    assert success_completed.is_error is False
    assert "工具执行失败" in success_completed.result or "成功结果" in success_completed.result

    # # 验证消息顺序与 tool_calls 一致
    second_call_messages = fake_runtime.last_calls[1]["messages"]
    tool_messages = [m for m in second_call_messages if m["role"] == "tool"]

    assert len(tool_messages) == 2
    assert tool_messages[0]["tool_call_id"] == "call_1"
    assert tool_messages[1]["tool_call_id"] == "call_2"


@pytest.mark.asyncio  # # 标记为异步测试
async def test_agent_loop_parallel_tool_calls_with_mixed_errors():
    """测试：并行执行中包含多种错误情况。

    验证当同时存在：JSON 解析错误、未知工具、执行错误时，
    所有工具都能正确处理，且顺序保持不变。
    """
    # # 只注册一个正常工具
    normal_tool = FakeTool(name="normal_tool", result=ToolResult(content="正常结果"))

    tool_registry = ToolRegistry()
    tool_registry.register(normal_tool)

    # # 第一轮：LLM 返回三个工具调用（未知工具、JSON错误、正常）
    chunks_round1 = [
        {
            "content": None,
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_1",
                    "function_name": "unknown_tool",  # # 未知工具
                    "arguments": '{"query": "test"}',
                },
                {
                    "index": 1,
                    "id": "call_2",
                    "function_name": "normal_tool",
                    "arguments": '{invalid json}',  # # JSON 解析错误
                },
                {
                    "index": 2,
                    "id": "call_3",
                    "function_name": "normal_tool",
                    "arguments": '{"query": "valid"}',  # # 正常
                },
            ],
        },
        {"finish_reason": "tool_calls"},
    ]

    # # 第二轮：LLM 返回最终结果
    chunks_round2 = [
        {"content": "完成"},
        {"finish_reason": "stop"},
    ]

    fake_runtime = FakeAgentRuntime(chunks_list=[chunks_round1, chunks_round2])

    # # 创建 AgentLoop 和执行配置
    agent_loop = AgentLoop()  # # 使用默认构造
    profile = build_test_profile(runtime=fake_runtime, tool_registry=tool_registry, max_turns=10)  # # 构造 profile

    # # 准备测试数据
    messages = [{"role": "user", "content": "测试"}]

    # # 收集事件
    events = []
    async for event in agent_loop.run(run_id="run-1", profile=profile, messages=messages):
        events.append(event)

    # # 验证所有 completed 事件
    completed_events = [e for e in events if isinstance(e, ToolUseCompletedEvent)]
    assert len(completed_events) == 3

    # # 验证顺序和错误状态
    # call_1: 未知工具
    assert completed_events[0].tool_call_id == "call_1"
    assert completed_events[0].is_error is True
    assert "未知工具" in completed_events[0].result

    # call_2: JSON 解析错误
    assert completed_events[1].tool_call_id == "call_2"
    assert completed_events[1].is_error is True
    assert "JSON 解析失败" in completed_events[1].result

    # call_3: 正常
    assert completed_events[2].tool_call_id == "call_3"
    assert completed_events[2].is_error is False
    assert completed_events[2].result == "正常结果"

    # # 验证对话消息顺序
    second_call_messages = fake_runtime.last_calls[1]["messages"]
    tool_messages = [m for m in second_call_messages if m["role"] == "tool"]

    assert len(tool_messages) == 3
    assert tool_messages[0]["tool_call_id"] == "call_1"
    assert tool_messages[1]["tool_call_id"] == "call_2"
    assert tool_messages[2]["tool_call_id"] == "call_3"


# =============================================================================
# 新增测试：profile-only 模式的回归测试
# =============================================================================


@pytest.mark.asyncio  # # 标记为异步测试
async def test_agent_loop_rejects_old_agent_argument_mode():
    """测试 AgentLoop.run 不再支持旧的独立 agent 参数模式。

    验证当使用旧式的 agent= 关键字参数调用时，会抛出 TypeError。
    """
    agent_loop = AgentLoop()  # # 使用默认构造
    with pytest.raises(TypeError):  # # 期望抛出 TypeError
        async for _event in agent_loop.run(  # # 尝试使用旧的 agent 参数调用
            run_id="run-1",
            agent=Agent(  # # 旧式的独立 agent 参数
                agent_id="old-agent",
                name="Old Agent",
                model="gpt-4.1-mini",
                system_prompt="old",
                temperature=0.2,
            ),
            messages=[],  # # 空消息列表
        ):
            pass  # # 不应到达这里
