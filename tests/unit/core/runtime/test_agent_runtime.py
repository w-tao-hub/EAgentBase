"""AgentRuntime 的单元测试。

测试 AgentRuntime 的 stream_once 方法，验证纯文本场景和 tool_calls 场景。
"""

from __future__ import annotations  # # 启用未来注解

import pytest  # # 导入 pytest 测试框架
from dataclasses import dataclass  # # 导入数据类装饰器
from typing import AsyncIterator, Any  # # 导入异步迭代器和任意类型

from app.core.models.agent import Agent  # # 导入 Agent 模型
from app.core.models.execution_context import ExecutionContext  # # 导入执行上下文模型
from app.core.models.llm_chunk import LLMChunk  # # 导入 LLMChunk
from app.core.hooks import (  # # 导入 Hook 相关模型
    HookExecutionError,
    ModelHook,
    ModelHookPipeline,
    ModelRequest,
    ModelResponse,
    NoOpStreamTextGuard,
    StreamTextGuard,
)
from app.core.runtime.agent_runtime import (  # # 导入被测类和数据类
    AgentRuntime,
    TurnComplete,
    ToolCall,
    Function,
    UsageInfo,
)


@dataclass  # # 定义为数据类
class FakeChunk(LLMChunk):  # # 继承自 LLMChunk，与真实适配器保持一致
    """模拟 LLM 返回的 chunk 对象。"""

    # # 继承 LLMChunk 的所有字段：
    # # - content: str | None = None
    # # - thinking: str | None = None
    # # - tool_calls: list[dict] | None = None
    # # - finish_reason: str | None = None
    # # - usage: UsageInfo | None = None
    pass  # # 无需额外定义，完全继承 LLMChunk


class FakeLLMAdapter:
    """模拟 LLM 适配器，用于测试。"""

    def __init__(
        self,
        chunks: list[FakeChunk] | None = None,  # # 要返回的 chunks
        raise_error: bool = False,  # # 是否抛出异常
        raise_transient_error: bool = False,  # # 是否抛出瞬态错误（用于测试重试）
        raise_transient_error_always: bool = False,  # # 是否每次调用都抛出瞬态错误（用于测试重试耗尽）
    ) -> None:  # # 构造函数
        """初始化模拟适配器。"""
        self.chunks = chunks or []  # # 保存要返回的 chunks
        self.raise_error = raise_error  # # 是否抛出异常
        self.raise_transient_error = raise_transient_error  # # 是否抛出瞬态错误
        self.raise_transient_error_always = raise_transient_error_always  # # 是否每次调用都抛出瞬态错误
        self.call_count = 0  # # 记录调用次数
        self.last_call: dict | None = None  # # 记录最后一次调用参数

    async def stream_completion(  # # 模拟流式完成方法
        self,
        model: str,  # # 模型名称
        messages: list[dict],  # # 消息列表
        temperature: float,  # # 温度参数
        api_key: str | None = None,  # # API 密钥
        tools: list[dict] | None = None,  # # 工具列表
        reasoning_effort: str | None = None,  # # thinking 模式的思考强度
    ) -> AsyncIterator[FakeChunk]:  # # 返回异步迭代器
        """模拟 LLM 流式调用。"""
        self.call_count += 1  # # 增加调用计数
        self.last_call = {  # # 记录调用参数
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "api_key": api_key,
            "tools": tools,
            "reasoning_effort": reasoning_effort,
        }

        # # 如果设置了抛出瞬态错误（仅第一次或每次都抛）
        if (self.raise_transient_error and self.call_count == 1) or self.raise_transient_error_always:  # # 抛出瞬态错误
            raise Exception("Transient error: connection reset")  # # 抛出瞬态错误

        if self.raise_error:  # # 如果设置了抛出异常
            raise Exception("LLM API error")  # # 抛出异常

        for chunk in self.chunks:  # # 遍历 chunks
            yield chunk  # # 生成模拟 chunk


class RecordingBeforeModelHook(ModelHook):
    """记录并改写模型请求的 Hook。"""

    async def before_model(self, request: ModelRequest, context: ExecutionContext) -> ModelRequest:
        """在模型调用前追加测试消息与工具。"""
        return ModelRequest(
            messages=request.messages + [{"role": "system", "content": "hook-added"}],
            tools=(request.tools or []) + [{"type": "function", "function": {"name": "hook_tool"}}],
            model=request.model,
            temperature=request.temperature,
        )


class RewriteUsageAfterModelHook(ModelHook):
    """改写模型响应 usage 的 Hook。"""

    async def after_model(self, response: ModelResponse, context: ExecutionContext) -> ModelResponse:
        """在模型调用后重写 usage，便于验证 after_model 已执行。"""
        return ModelResponse(
            text=response.text,
            tool_calls=response.tool_calls,
            usage=UsageInfo(prompt_tokens=99, completion_tokens=88, total_tokens=187),
        )


class FailClosedModelHook(ModelHook):
    """始终失败的 fail-closed 模型 Hook。"""

    def __init__(self) -> None:
        """初始化 Hook，并声明失败时中断主流程。"""
        super().__init__(fail_open=False)

    async def before_model(self, request: ModelRequest, context: ExecutionContext) -> ModelRequest:
        """抛出异常，验证 fail-closed 行为。"""
        raise RuntimeError("model hook boom")


class RecordingStreamTextGuard(StreamTextGuard):
    """记录文本分片流经情况的守卫。"""

    def __init__(self) -> None:
        """初始化记录容器。"""
        self.seen_chunks: list[str] = []
        self.flushed = False

    async def ingest_text(self, chunk: str, context: ExecutionContext) -> list[str]:
        """记录传入文本，并原样透传。"""
        self.seen_chunks.append(chunk)
        return [chunk]

    async def flush(self, context: ExecutionContext) -> list[str]:
        """记录 flush 调用，不补发额外文本。"""
        self.flushed = True
        return []


@pytest.mark.asyncio  # # 标记为异步测试
async def test_stream_once_text_only():
    """测试：stream_once 纯文本场景，返回 str 和 TurnComplete。"""
    # # 准备模拟 chunks，模拟纯文本流式响应
    chunks = [  # # 构造模拟 chunks
        FakeChunk(content="Hello"),  # # 第一个文本片段
        FakeChunk(content=" "),  # # 第二个文本片段
        FakeChunk(content="world"),  # # 第三个文本片段
        FakeChunk(content="", finish_reason="stop", usage=UsageInfo(prompt_tokens=10, completion_tokens=3, total_tokens=13)),  # # 结束 chunk
    ]
    fake_adapter = FakeLLMAdapter(chunks=chunks)  # # 创建模拟适配器
    runtime = AgentRuntime(llm_adapter=fake_adapter)  # # 创建运行时实例

    # # 准备测试数据
    agent = Agent(  # # 构造 Agent 实例
        agent_id="test-agent",
        name="Test Agent",
        model="gpt-4.1-mini",
        system_prompt="You are helpful.",
        temperature=0.2,
    )
    messages = [{"role": "user", "content": "Say hello"}]  # # 构造消息列表

    # # 收集结果
    results = []  # # 初始化结果列表
    full_text = ""  # # 手动累加文本
    async for item in runtime.stream_once(agent, messages):  # # 调用 stream_once
        results.append(item)  # # 收集结果
        if isinstance(item, str):  # # 如果是文本片段
            full_text += item  # # 累加文本

    # # 验证结果
    assert len(results) == 4  # # 断言结果数量：3 个 str + 1 个 TurnComplete

    # # 验证 str 文本片段
    for i, expected_content in enumerate(["Hello", " ", "world"]):  # # 遍历预期的文本片段
        assert isinstance(results[i], str)  # # 断言类型为 str
        assert results[i] == expected_content  # # 断言内容正确

    # # 验证 TurnComplete
    turn_complete = results[-1]  # # 获取最后一个结果
    assert isinstance(turn_complete, TurnComplete)  # # 断言类型为 TurnComplete
    assert turn_complete.tool_calls is None  # # 断言没有工具调用
    assert turn_complete.usage is not None  # # 断言有用量信息
    assert turn_complete.usage.prompt_tokens == 10  # # 断言 prompt_tokens 正确
    assert turn_complete.usage.completion_tokens == 3  # # 断言 completion_tokens 正确
    assert turn_complete.reasoning_content is None  # # 纯文本场景不会附带 reasoning_content

    # # 验证手动累加的文本
    assert full_text == "Hello world"  # # 断言完整文本正确


@pytest.mark.asyncio  # # 标记为异步测试
async def test_stream_once_with_tool_calls():
    """测试：stream_once 含 tool_calls 场景。"""
    # # 准备模拟 chunks，模拟工具调用流式响应
    # # 使用平铺格式：{"index": 0, "id": "...", "function_name": "...", "arguments": "..."}
    tool_call_chunks = [  # # 构造工具调用 chunks
        FakeChunk(content=""),  # # 空内容 chunk
        FakeChunk(  # # 第一个工具调用 chunk
            tool_calls=[
                {
                    "index": 0,
                    "id": "call_1",
                    "function_name": "get_weather",
                    "arguments": '{"city": "',
                }
            ]
        ),
        FakeChunk(  # # 第二个工具调用 chunk（参数续传）
            tool_calls=[
                {
                    "index": 0,
                    "arguments": 'Beijing"}',
                }
            ]
        ),
        FakeChunk(  # # 结束 chunk
            content="",
            finish_reason="tool_calls",
            usage=UsageInfo(prompt_tokens=15, completion_tokens=20, total_tokens=35),
        ),
    ]
    fake_adapter = FakeLLMAdapter(chunks=tool_call_chunks)  # # 创建模拟适配器
    runtime = AgentRuntime(llm_adapter=fake_adapter)  # # 创建运行时实例

    # # 准备测试数据
    agent = Agent(  # # 构造 Agent 实例
        agent_id="test-agent",
        name="Test Agent",
        model="gpt-4.1-mini",
        system_prompt="You are helpful.",
        temperature=0.2,
    )
    messages = [{"role": "user", "content": "What's the weather?"}]  # # 构造消息列表
    tools = [  # # 构造工具列表
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather for a city",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }
    ]

    # # 收集结果
    results = []  # # 初始化结果列表
    async for item in runtime.stream_once(agent, messages, tools=tools):  # # 调用 stream_once
        results.append(item)  # # 收集结果

    # # 验证结果
    # # 空 content 不会 yield str（AgentRuntime 中 if content: 过滤），所以只有 1 个 TurnComplete
    assert len(results) == 1  # # 断言结果数量：1 个 TurnComplete

    # # 验证 TurnComplete
    turn_complete = results[-1]  # # 获取最后一个结果
    assert isinstance(turn_complete, TurnComplete)  # # 断言类型为 TurnComplete
    assert turn_complete.usage is not None  # # 断言有用量信息
    assert turn_complete.usage.prompt_tokens == 15  # # 断言 prompt_tokens 正确
    assert turn_complete.usage.completion_tokens == 20  # # 断言 completion_tokens 正确

    # # 验证 tool_calls
    assert turn_complete.tool_calls is not None  # # 断言有工具调用
    assert len(turn_complete.tool_calls) == 1  # # 断言工具调用数量为 1
    assert turn_complete.tool_calls[0].id == "call_1"  # # 断言工具调用 ID 正确
    assert turn_complete.tool_calls[0].type == "function"  # # 断言类型正确
    assert turn_complete.tool_calls[0].function.name == "get_weather"  # # 断言函数名正确（使用嵌套 function）
    assert turn_complete.tool_calls[0].function.arguments == '{"city": "Beijing"}'  # # 断言参数正确（已合并）
    assert fake_adapter.last_call is not None  # # 断言适配器实际收到了思考强度参数
    assert fake_adapter.last_call["reasoning_effort"] == "high"  # # 默认 Agent 思考强度会被透传给适配器


@pytest.mark.asyncio
async def test_stream_once_accumulates_reasoning_content_without_emitting_to_user():
    """测试：thinking 分片会累计到 TurnComplete，但不会作为文本片段直接输出。"""
    fake_adapter = FakeLLMAdapter(
        chunks=[
            FakeChunk(thinking="先思考"),
            FakeChunk(content="最终"),
            FakeChunk(thinking="，再补充"),
            FakeChunk(content="答案"),
            FakeChunk(finish_reason="stop", usage=UsageInfo(prompt_tokens=5, completion_tokens=2, total_tokens=7)),
        ]
    )
    runtime = AgentRuntime(llm_adapter=fake_adapter)
    agent = Agent(
        agent_id="test-agent",
        name="Test Agent",
        model="deepseek/deepseek-v4-flash",
        system_prompt="You are helpful.",
        temperature=0.2,
    )

    results = []
    async for item in runtime.stream_once(agent, [{"role": "user", "content": "hi"}]):
        results.append(item)

    text_results = [item for item in results if isinstance(item, str)]
    turn_complete = results[-1]

    assert text_results == ["最终", "答案"]
    assert isinstance(turn_complete, TurnComplete)
    assert turn_complete.reasoning_content == "先思考，再补充"


@pytest.mark.asyncio  # # 标记为异步测试
async def test_stream_once_with_tool_call_dicts():
    """测试：stream_once 能正确处理工具调用字典增量。

    这个用例模拟 LiteLLMAdapter 产出的 dict 形态工具调用增量，
    验证真实串联路径。
    """
    # # 准备工具调用增量序列（dict 格式）：
    # # 第一个片段只给出 id 和函数名，后续片段只通过相同 index 续传参数。
    tool_call_chunks = [
        FakeChunk(
            tool_calls=[
                {
                    "index": 0,
                    "id": "call_1",
                    "function_name": "grep_file",
                }
            ]
        ),
        FakeChunk(
            tool_calls=[
                {
                    "index": 0,
                    "arguments": '{"pattern": "',
                }
            ]
        ),
        FakeChunk(
            tool_calls=[
                {
                    "index": 0,
                    "arguments": 'hello", "path": "README.md"}',
                }
            ]
        ),
        FakeChunk(
            finish_reason="tool_calls",
            content=None,
        ),
    ]
    fake_adapter = FakeLLMAdapter(chunks=tool_call_chunks)  # # 创建模拟适配器
    runtime = AgentRuntime(llm_adapter=fake_adapter)  # # 创建运行时实例

    # # 准备测试数据
    agent = Agent(
        agent_id="test-agent",
        name="Test Agent",
        model="gpt-4.1-mini",
        system_prompt="You are helpful.",
        temperature=0.2,
    )
    messages = [{"role": "user", "content": "Search README"}]
    tools = [{"type": "function", "function": {"name": "grep_file"}}]

    # # 收集结果
    results = []
    async for item in runtime.stream_once(agent, messages, tools=tools):
        results.append(item)

    # # 验证最终只归并出一个完整工具调用。
    assert len(results) == 1
    turn_complete = results[-1]
    assert isinstance(turn_complete, TurnComplete)
    assert turn_complete.tool_calls is not None
    assert len(turn_complete.tool_calls) == 1
    assert turn_complete.tool_calls[0].id == "call_1"
    assert turn_complete.tool_calls[0].type == "function"
    assert turn_complete.tool_calls[0].function.name == "grep_file"
    assert turn_complete.tool_calls[0].function.arguments == '{"pattern": "hello", "path": "README.md"}'


@pytest.mark.asyncio  # # 标记为异步测试
async def test_stream_once_with_retry():
    """测试：stream_once 遇到瞬态错误时自动重试。"""
    # # 准备模拟适配器，第一次调用抛出瞬态错误，第二次成功
    chunks = [  # # 构造模拟 chunks
        FakeChunk(content="Hello"),  # # 文本片段
        FakeChunk(content="", finish_reason="stop", usage=UsageInfo(prompt_tokens=5, completion_tokens=1, total_tokens=6)),  # # 结束 chunk
    ]
    fake_adapter = FakeLLMAdapter(chunks=chunks, raise_transient_error=True)  # # 创建会抛出瞬态错误的模拟适配器
    runtime = AgentRuntime(llm_adapter=fake_adapter)  # # 创建运行时实例

    # # 准备测试数据
    agent = Agent(  # # 构造 Agent 实例
        agent_id="test-agent",
        name="Test Agent",
        model="gpt-4.1-mini",
        system_prompt="You are helpful.",
        temperature=0.2,
    )
    messages = [{"role": "user", "content": "Say hello"}]  # # 构造消息列表

    # # 收集结果
    results = []  # # 初始化结果列表
    full_text = ""  # # 手动累加文本
    async for item in runtime.stream_once(agent, messages):  # # 调用 stream_once
        results.append(item)  # # 收集结果
        if isinstance(item, str):  # # 如果是文本片段
            full_text += item  # # 累加文本

    # # 验证重试逻辑
    assert fake_adapter.call_count == 2  # # 断言适配器被调用了 2 次（第一次失败，第二次重试成功）

    # # 验证结果
    assert len(results) == 2  # # 断言结果数量：1 个 str + 1 个 TurnComplete
    assert isinstance(results[0], str)  # # 断言第一个是 str
    assert results[0] == "Hello"  # # 断言内容正确
    assert isinstance(results[1], TurnComplete)  # # 断言第二个是 TurnComplete

    # # 验证手动累加的文本
    assert full_text == "Hello"  # # 断言完整文本正确


@pytest.mark.asyncio  # # 标记为异步测试
async def test_stream_once_retry_exhausted():
    """测试：stream_once 遇到瞬态错误耗尽重试后抛出异常。"""
    # # 准备模拟适配器，每次调用都抛出瞬态错误
    fake_adapter = FakeLLMAdapter(raise_transient_error_always=True)  # # 创建始终抛出瞬态错误的模拟适配器
    runtime = AgentRuntime(llm_adapter=fake_adapter)  # # 创建运行时实例

    # # 准备测试数据
    agent = Agent(  # # 构造 Agent 实例
        agent_id="test-agent",
        name="Test Agent",
        model="gpt-4.1-mini",
        system_prompt="You are helpful.",
        temperature=0.2,
    )
    messages = [{"role": "user", "content": "Say hello"}]  # # 构造消息列表

    # # 验证抛出异常
    with pytest.raises(Exception, match="connection reset"):  # # 断言抛出异常
        async for _ in runtime.stream_once(agent, messages):  # # 调用 stream_once
            pass  # # 不收集结果

    # # 验证调用次数（_max_retries=2，初始 1 次 + 重试 1 次 = 2 次）
    assert fake_adapter.call_count == 2  # # 断言适配器被调用了 2 次


@pytest.mark.asyncio  # # 标记为异步测试
async def test_stream_once_passes_tools_to_llm():
    """测试：stream_once 正确传递 tools 参数给 LLM 适配器。"""
    chunks = [  # # 构造模拟 chunks
        FakeChunk(content="Done", finish_reason="stop", usage=None),  # # 结束 chunk
    ]
    fake_adapter = FakeLLMAdapter(chunks=chunks)  # # 创建模拟适配器
    runtime = AgentRuntime(llm_adapter=fake_adapter)  # # 创建运行时实例

    # # 准备测试数据
    agent = Agent(  # # 构造 Agent 实例
        agent_id="test-agent",
        name="Test Agent",
        model="gpt-4.1-mini",
        system_prompt="You are helpful.",
        temperature=0.2,
    )
    messages = [{"role": "user", "content": "Do something"}]  # # 构造消息列表
    tools = [{"type": "function", "function": {"name": "test_tool"}}]  # # 构造工具列表

    # # 执行调用
    async for _ in runtime.stream_once(agent, messages, tools=tools):  # # 调用 stream_once
        pass  # # 不收集结果

    # # 验证传递给 LLM 的参数
    assert fake_adapter.last_call is not None  # # 断言适配器被调用
    assert fake_adapter.last_call["tools"] == tools  # # 断言 tools 参数正确传递


@pytest.mark.asyncio  # # 标记为异步测试
async def test_stream_once_empty_response():
    """测试：stream_once 处理空响应场景。"""
    chunks = [  # # 构造模拟 chunks
        FakeChunk(content="", finish_reason="stop", usage=UsageInfo(prompt_tokens=5, completion_tokens=0, total_tokens=5)),  # # 空响应结束 chunk
    ]
    fake_adapter = FakeLLMAdapter(chunks=chunks)  # # 创建模拟适配器
    runtime = AgentRuntime(llm_adapter=fake_adapter)  # # 创建运行时实例

    # # 准备测试数据
    agent = Agent(  # # 构造 Agent 实例
        agent_id="test-agent",
        name="Test Agent",
        model="gpt-4.1-mini",
        system_prompt="You are helpful.",
        temperature=0.2,
    )
    messages = [{"role": "user", "content": "Say nothing"}]  # # 构造消息列表

    # # 收集结果
    results = []  # # 初始化结果列表
    async for item in runtime.stream_once(agent, messages):  # # 调用 stream_once
        results.append(item)  # # 收集结果

    # # 验证结果
    assert len(results) == 1  # # 断言结果数量：只有 1 个 TurnComplete
    assert isinstance(results[0], TurnComplete)  # # 断言类型为 TurnComplete
    assert results[0].tool_calls is None  # # 断言没有工具调用
    assert results[0].usage is not None  # # 断言有用量信息


@pytest.mark.asyncio  # # 标记为异步测试
async def test_stream_once_runs_model_hooks_and_stream_guard():
    """测试：stream_once 会执行模型 Hook，并把文本交给流式守卫。"""
    chunks = [
        FakeChunk(content="Hello"),
        FakeChunk(content="", finish_reason="stop", usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2)),
    ]
    fake_adapter = FakeLLMAdapter(chunks=chunks)
    guard = RecordingStreamTextGuard()
    runtime = AgentRuntime(
        llm_adapter=fake_adapter,
        model_hook_pipeline=ModelHookPipeline(
            hooks=[RecordingBeforeModelHook(), RewriteUsageAfterModelHook()]
        ),
        stream_text_guard=guard,
    )

    agent = Agent(
        agent_id="test-agent",
        name="Test Agent",
        model="gpt-4.1-mini",
        system_prompt="You are helpful.",
        temperature=0.2,
    )
    context = ExecutionContext(
        run_id="run-1",
        session_id="session-1",
        metadata={"trace_id": "req-1"},
        agent=agent,
    )
    messages = [{"role": "user", "content": "Say hello"}]
    tools = [{"type": "function", "function": {"name": "orig_tool"}}]

    results = []
    async for item in runtime.stream_once(agent, messages, tools=tools, context=context):
        results.append(item)

    assert fake_adapter.last_call is not None
    assert fake_adapter.last_call["messages"][-1] == {"role": "system", "content": "hook-added"}
    assert fake_adapter.last_call["tools"][-1]["function"]["name"] == "hook_tool"
    assert guard.seen_chunks == ["Hello"]
    assert guard.flushed is True
    assert results[0] == "Hello"
    assert isinstance(results[-1], TurnComplete)
    assert results[-1].usage == UsageInfo(prompt_tokens=99, completion_tokens=88, total_tokens=187)


@pytest.mark.asyncio  # # 标记为异步测试
async def test_stream_once_raises_hook_execution_error_when_fail_closed_model_hook_fails():
    """测试：fail-closed 的模型 Hook 失败时，stream_once 抛出 HookExecutionError。"""
    fake_adapter = FakeLLMAdapter(chunks=[FakeChunk(content="unused")])
    runtime = AgentRuntime(
        llm_adapter=fake_adapter,
        model_hook_pipeline=ModelHookPipeline(hooks=[FailClosedModelHook()]),
        stream_text_guard=NoOpStreamTextGuard(),
    )

    agent = Agent(
        agent_id="test-agent",
        name="Test Agent",
        model="gpt-4.1-mini",
        system_prompt="You are helpful.",
        temperature=0.2,
    )
    context = ExecutionContext(
        run_id="run-1",
        session_id="session-1",
        metadata=None,
        agent=agent,
    )

    with pytest.raises(HookExecutionError, match="model hook boom"):
        async for _ in runtime.stream_once(
            agent,
            [{"role": "user", "content": "Say hello"}],
            context=context,
        ):
            pass
