"""LiteLLMAdapter 的单元测试。

测试 LiteLLM 适配器的 chunk 归一化和异常收敛功能。
新增测试：tool_calls 增量片段解析。
"""

from __future__ import annotations  # 启用未来注解

import sys  # 导入 sys，用于替换延迟导入的 litellm 模块
from types import SimpleNamespace  # 导入简单命名空间，便于构造假模块

import pytest  # 导入 pytest 测试框架

from app.core.models.llm_chunk import LLMChunk  # 导入 LLMChunk 模型
from app.infra.llm.litellm_adapter import LiteLLMAdapter  # 导入被测类


class FakeLiteLLMResponse:
    """模拟 LiteLLM 流式响应的 chunk 对象（纯文本内容）。"""

    def __init__(self, content: str | None) -> None:  # 构造函数
        """初始化模拟响应对象。"""
        self.choices = [self._Choice(content)]  # 构造 choices 列表

    class _Choice:
        """模拟 LiteLLM 的 choice 对象。"""

        def __init__(self, content: str | None) -> None:  # 构造函数
            """初始化模拟 choice 对象。"""
            self.delta = self._Delta(content)  # 构造 delta 对象
            self.finish_reason = None  # 纯文本响应没有 finish_reason

        class _Delta:
            """模拟 LiteLLM 的 delta 对象。"""

            def __init__(self, content: str | None) -> None:  # 构造函数
                """初始化模拟 delta 对象。"""
                self.content = content  # 设置 content 字段
                self.tool_calls = None  # 纯文本响应没有 tool_calls


class FakeLiteLLMResponseWithToolCalls:
    """模拟 LiteLLM 流式响应的 chunk 对象（含 tool_calls）。"""

    def __init__(
        self,
        content: str | None = None,  # 文本内容
        tool_calls: list[FakeToolCall] | None = None,  # 工具调用列表
        finish_reason: str | None = None,  # 完成原因
    ) -> None:
        """初始化模拟响应对象。"""
        self.choices = [self._Choice(content, tool_calls, finish_reason)]  # 构造 choices 列表

    class _Choice:
        """模拟 LiteLLM 的 choice 对象。"""

        def __init__(
            self,
            content: str | None,
            tool_calls: list[FakeToolCall] | None,
            finish_reason: str | None,
        ) -> None:
            """初始化模拟 choice 对象。"""
            self.delta = self._Delta(content, tool_calls)  # 构造 delta 对象
            self.finish_reason = finish_reason  # 设置完成原因

        class _Delta:
            """模拟 LiteLLM 的 delta 对象。"""

            def __init__(
                self,
                content: str | None,
                tool_calls: list[FakeToolCall] | None,
            ) -> None:
                """初始化模拟 delta 对象。"""
                self.content = content  # 设置 content 字段
                self.tool_calls = tool_calls  # 设置 tool_calls 字段


class FakeToolCall:
    """模拟 LiteLLM 的 tool_call 对象。"""

    def __init__(
        self,
        index: int = 0,  # 工具调用索引
        tc_id: str | None = None,  # 工具调用 ID
        function_name: str | None = None,  # 函数名
        arguments: str | None = None,  # 参数
    ) -> None:
        """初始化模拟 tool_call 对象。"""
        self.index = index  # 设置索引
        self.id = tc_id  # 设置 ID
        # 构造 function 对象
        if function_name is not None or arguments is not None:  # 如果有函数信息
            self.function = self._Function(function_name, arguments)  # 创建 function 对象
        else:
            self.function = None  # 没有函数信息

    class _Function:
        """模拟 LiteLLM 的 function 对象。"""

        def __init__(
            self,
            name: str | None = None,  # 函数名
            arguments: str | None = None,  # 参数
        ) -> None:
            """初始化模拟 function 对象。"""
            self.name = name  # 设置函数名
            self.arguments = arguments  # 设置参数


async def build_fake_stream(chunks: list[object]):
    """构造假的 LiteLLM 异步流。

    Args:
        chunks: 需要按顺序产出的 fake chunk 列表
    """
    for chunk in chunks:  # 逐个产出 fake chunk
        yield chunk  # 模拟 LiteLLM 的流式返回


def install_fake_litellm(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response_stream: object,
) -> SimpleNamespace:
    """安装假的 litellm 模块，避免在生产代码中保留测试入口。

    Args:
        monkeypatch: pytest 提供的 monkeypatch fixture
        response_stream: `acompletion()` 返回的异步迭代器

    Returns:
        安装后的假 litellm 模块对象，便于断言超时等设置
    """

    fake_module = SimpleNamespace(  # 构造最小可用的假模块
        request_timeout=None,  # 预留给适配器写入超时时间
        acompletion=None,  # 先占位，后面再绑定具体实现
        last_acompletion_kwargs=None,  # 保存最近一次调用参数，便于断言请求体
    )

    async def fake_acompletion(**kwargs):  # 模拟 LiteLLM 的异步完成接口
        fake_module.last_acompletion_kwargs = kwargs  # 记录最近一次调用参数，便于断言 DeepSeek thinking 请求体
        return response_stream  # 直接返回预设的异步流

    fake_module.acompletion = fake_acompletion  # 绑定假的 acompletion 实现
    monkeypatch.setitem(sys.modules, "litellm", fake_module)  # 替换延迟导入的 litellm
    return fake_module  # 返回给调用方做补充断言


@pytest.mark.asyncio  # 标记为异步测试
async def test_litellm_adapter_yields_normalized_chunks(monkeypatch: pytest.MonkeyPatch):
    """测试：适配器将 LiteLLM chunk 归一化为统一格式（纯文本）。"""
    adapter = LiteLLMAdapter(timeout_seconds=60)  # 创建适配器实例，测试真实路径

    # 模拟 LiteLLM 返回的 chunks
    fake_chunks = [  # 构造模拟 chunk 列表
        FakeLiteLLMResponse("Hello"),  # 第一个 chunk
        FakeLiteLLMResponse(" "),  # 第二个 chunk
        FakeLiteLLMResponse("world"),  # 第三个 chunk
        FakeLiteLLMResponse(None),  # 结束 chunk（content 为 None）
    ]
    fake_litellm = install_fake_litellm(  # 安装假的 litellm 模块
        monkeypatch,
        response_stream=build_fake_stream(fake_chunks),
    )

    # 收集归一化后的 chunks
    chunks = []  # 初始化 chunk 列表
    async for chunk in adapter.stream_completion(  # 调用适配器流式方法
        model="gpt-4.1-mini",  # 模型名称
        messages=[{"role": "user", "content": "hi"}],  # 消息列表
        temperature=0.2,  # 温度参数
        api_key="fake-key",  # API 密钥
    ):
        chunks.append(chunk)  # 收集 chunk

    # 验证归一化结果
    assert len(chunks) == 3  # 断言过滤掉了 None content 的 chunk
    assert all(isinstance(c, LLMChunk) for c in chunks)  # 断言所有 chunk 都是 LLMChunk 类型
    assert chunks[0].content == "Hello"  # 断言第一个 chunk 内容
    assert chunks[1].content == " "  # 断言第二个 chunk 内容
    assert chunks[2].content == "world"  # 断言第三个 chunk 内容
    assert all(c.tool_calls is None for c in chunks)  # 断言所有 chunk 都没有 tool_calls
    assert all(c.finish_reason is None for c in chunks)  # 断言所有 chunk 都没有 finish_reason
    assert fake_litellm.request_timeout == 60  # 断言适配器仍会把超时写入 LiteLLM


@pytest.mark.asyncio
async def test_litellm_adapter_stream_completion_enables_deepseek_thinking(monkeypatch: pytest.MonkeyPatch):
    """测试：DeepSeek 主对话流式调用会显式开启 thinking，并透传 reasoning_effort。"""
    adapter = LiteLLMAdapter(timeout_seconds=60)
    fake_litellm = install_fake_litellm(
        monkeypatch,
        response_stream=build_fake_stream([FakeLiteLLMResponse("ok")]),
    )

    chunks = []
    async for chunk in adapter.stream_completion(
        model="deepseek/deepseek-v4-flash",
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.2,
        reasoning_effort="max",
    ):
        chunks.append(chunk)

    assert [chunk.content for chunk in chunks] == ["ok"]
    assert fake_litellm.last_acompletion_kwargs is not None
    assert fake_litellm.last_acompletion_kwargs["extra_body"] == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "max",
    }


@pytest.mark.asyncio  # 标记为异步测试
async def test_litellm_adapter_propagates_exception(monkeypatch: pytest.MonkeyPatch):
    """测试：适配器将 LiteLLM 异常向上传播，由调用方（AgentRuntime）统一收敛。"""
    adapter = LiteLLMAdapter(timeout_seconds=60)  # 创建适配器实例，测试真实路径

    # 模拟抛出异常的异步生成器
    async def error_chunks():  # 定义异步生成器抛出异常
        raise Exception("LiteLLM connection error")  # 抛出异常
        yield None  # 永远不会执行到这里

    install_fake_litellm(  # 安装假的 litellm 模块，异常在流式遍历阶段抛出
        monkeypatch,
        response_stream=error_chunks(),
    )

    # 验证异常被向上传播（由 AgentRuntime 负责收敛为 run_failed 事件）
    with pytest.raises(Exception, match="LiteLLM connection error"):  # 期望异常被传播
        async for chunk in adapter.stream_completion(  # 调用流式完成方法
            model="gpt-4.1-mini",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.2,
            api_key="fake-key",
        ):
            pass  # 不应到达这里


# ============ 新增测试：tool_calls 解析测试（红-绿测试） ============


@pytest.mark.asyncio  # 标记为异步测试
async def test_litellm_adapter_parses_tool_calls_delta(monkeypatch: pytest.MonkeyPatch):
    """测试：适配器正确解析含 tool_calls 的流式 chunk。

    红测试：验证适配器能够解析包含工具调用增量片段的 chunk。
    """
    adapter = LiteLLMAdapter(timeout_seconds=60)  # 创建适配器实例，测试真实路径

    # 模拟包含 tool_calls 的 chunks
    fake_chunks = [  # 构造模拟 chunk 列表
        # 第一个 chunk：开始工具调用，提供 ID 和函数名
        FakeLiteLLMResponseWithToolCalls(
            content=None,  # 没有文本内容
            tool_calls=[  # 工具调用增量
                FakeToolCall(
                    index=0,  # 第一个工具调用
                    tc_id="call_abc123",  # 工具调用 ID
                    function_name="get_weather",  # 函数名
                    arguments=None,  # 还没有参数
                ),
            ],
        ),
        # 第二个 chunk：参数片段 1
        FakeLiteLLMResponseWithToolCalls(
            content=None,
            tool_calls=[
                FakeToolCall(
                    index=0,
                    tc_id=None,  # ID 只在第一个 chunk 提供
                    function_name=None,  # 函数名只在第一个 chunk 提供
                    arguments='{"location": "',  # 参数片段
                ),
            ],
        ),
        # 第三个 chunk：参数片段 2
        FakeLiteLLMResponseWithToolCalls(
            content=None,
            tool_calls=[
                FakeToolCall(
                    index=0,
                    tc_id=None,
                    function_name=None,
                    arguments='Beijing"}',  # 参数片段完成
                ),
            ],
        ),
        # 第四个 chunk：完成原因
        FakeLiteLLMResponseWithToolCalls(
            content=None,
            tool_calls=None,
            finish_reason="tool_calls",  # 完成原因：工具调用
        ),
    ]
    install_fake_litellm(  # 安装假的 litellm 模块
        monkeypatch,
        response_stream=build_fake_stream(fake_chunks),
    )

    # 收集归一化后的 chunks
    chunks = []  # 初始化 chunk 列表
    async for chunk in adapter.stream_completion(  # 调用适配器流式方法
        model="gpt-4.1-mini",  # 模型名称
        messages=[{"role": "user", "content": "What's the weather?"}],  # 消息列表
        temperature=0.2,  # 温度参数
        api_key="fake-key",  # API 密钥
        tools=[  # 提供工具定义
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather information",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "location": {"type": "string"},
                        },
                    },
                },
            },
        ],
    ):
        chunks.append(chunk)  # 收集 chunk

    # 验证结果
    assert len(chunks) == 4  # 断言有 4 个 chunk

    # 验证第一个 chunk：ID 和函数名
    assert chunks[0].content is None  # 没有文本内容
    assert chunks[0].tool_calls is not None  # 有工具调用
    assert len(chunks[0].tool_calls) == 1  # 有一个工具调用
    assert chunks[0].tool_calls[0]["index"] == 0  # 索引为 0
    assert chunks[0].tool_calls[0]["id"] == "call_abc123"  # ID 正确
    assert chunks[0].tool_calls[0]["function_name"] == "get_weather"  # 函数名正确
    assert chunks[0].tool_calls[0].get("arguments") is None  # 还没有参数
    assert chunks[0].finish_reason is None  # 没有完成原因

    # 验证第二个 chunk：参数片段 1
    assert chunks[1].content is None
    assert chunks[1].tool_calls is not None
    assert len(chunks[1].tool_calls) == 1
    assert chunks[1].tool_calls[0]["index"] == 0
    assert chunks[1].tool_calls[0].get("id") is None  # ID 只在第一个 chunk 提供
    assert chunks[1].tool_calls[0].get("function_name") is None  # 函数名只在第一个 chunk 提供
    assert chunks[1].tool_calls[0]["arguments"] == '{"location": "'  # 参数片段正确

    # 验证第三个 chunk：参数片段 2
    assert chunks[2].content is None
    assert chunks[2].tool_calls is not None
    assert chunks[2].tool_calls[0]["arguments"] == 'Beijing"}'  # 参数片段正确

    # 验证第四个 chunk：完成原因
    assert chunks[3].content is None
    assert chunks[3].tool_calls is None
    assert chunks[3].finish_reason == "tool_calls"  # 完成原因为 tool_calls


@pytest.mark.asyncio  # 标记为异步测试
async def test_litellm_adapter_parses_mixed_content_and_tool_calls(monkeypatch: pytest.MonkeyPatch):
    """测试：适配器正确解析同时包含文本和工具调用的 chunk。

    某些模型可能会在工具调用前输出一些文本说明。
    """
    adapter = LiteLLMAdapter(timeout_seconds=60)  # 创建适配器实例，测试真实路径

    # 模拟同时包含 content 和 tool_calls 的 chunks
    fake_chunks = [  # 构造模拟 chunk 列表
        # 第一个 chunk：文本说明
        FakeLiteLLMResponseWithToolCalls(
            content="I'll check the weather for you.",  # 文本内容
            tool_calls=None,
        ),
        # 第二个 chunk：开始工具调用
        FakeLiteLLMResponseWithToolCalls(
            content=None,
            tool_calls=[
                FakeToolCall(
                    index=0,
                    tc_id="call_xyz789",
                    function_name="get_weather",
                    arguments='{"location": "Shanghai"}',
                ),
            ],
        ),
        # 第三个 chunk：完成
        FakeLiteLLMResponseWithToolCalls(
            content=None,
            tool_calls=None,
            finish_reason="tool_calls",
        ),
    ]
    install_fake_litellm(  # 安装假的 litellm 模块
        monkeypatch,
        response_stream=build_fake_stream(fake_chunks),
    )

    # 收集归一化后的 chunks
    chunks = []  # 初始化 chunk 列表
    async for chunk in adapter.stream_completion(  # 调用适配器流式方法
        model="gpt-4.1-mini",
        messages=[{"role": "user", "content": "Weather in Shanghai?"}],
        temperature=0.2,
        api_key="fake-key",
    ):
        chunks.append(chunk)  # 收集 chunk

    # 验证结果
    assert len(chunks) == 3  # 断言有 3 个 chunk

    # 验证第一个 chunk：文本内容
    assert chunks[0].content == "I'll check the weather for you."  # 文本内容正确
    assert chunks[0].tool_calls is None  # 没有工具调用

    # 验证第二个 chunk：工具调用
    assert chunks[1].content is None
    assert chunks[1].tool_calls is not None
    assert chunks[1].tool_calls[0]["function_name"] == "get_weather"
    assert chunks[1].tool_calls[0]["arguments"] == '{"location": "Shanghai"}'

    # 验证第三个 chunk：完成原因
    assert chunks[2].finish_reason == "tool_calls"


@pytest.mark.asyncio  # 标记为异步测试
async def test_litellm_adapter_parses_multiple_tool_calls(monkeypatch: pytest.MonkeyPatch):
    """测试：适配器正确解析多个工具调用的 chunk。

    某些场景下模型可能一次调用多个工具。
    """
    adapter = LiteLLMAdapter(timeout_seconds=60)  # 创建适配器实例，测试真实路径

    # 模拟包含多个 tool_calls 的 chunk
    fake_chunks = [  # 构造模拟 chunk 列表
        FakeLiteLLMResponseWithToolCalls(
            content=None,
            tool_calls=[
                FakeToolCall(
                    index=0,
                    tc_id="call_1",
                    function_name="get_weather",
                    arguments='{"location": "Beijing"}',
                ),
                FakeToolCall(
                    index=1,
                    tc_id="call_2",
                    function_name="get_time",
                    arguments='{"timezone": "UTC"}',
                ),
            ],
        ),
    ]
    install_fake_litellm(  # 安装假的 litellm 模块
        monkeypatch,
        response_stream=build_fake_stream(fake_chunks),
    )

    # 收集归一化后的 chunks
    chunks = []  # 初始化 chunk 列表
    async for chunk in adapter.stream_completion(  # 调用适配器流式方法
        model="gpt-4.1-mini",
        messages=[{"role": "user", "content": "Weather and time?"}],
        temperature=0.2,
        api_key="fake-key",
    ):
        chunks.append(chunk)  # 收集 chunk

    # 验证结果
    assert len(chunks) == 1  # 断言有 1 个 chunk
    assert chunks[0].tool_calls is not None
    assert len(chunks[0].tool_calls) == 2  # 有两个工具调用

    # 验证第一个工具调用
    assert chunks[0].tool_calls[0]["index"] == 0
    assert chunks[0].tool_calls[0]["id"] == "call_1"
    assert chunks[0].tool_calls[0]["function_name"] == "get_weather"

    # 验证第二个工具调用
    assert chunks[0].tool_calls[1]["index"] == 1
    assert chunks[0].tool_calls[1]["id"] == "call_2"
    assert chunks[0].tool_calls[1]["function_name"] == "get_time"


@pytest.mark.asyncio  # 标记为异步测试
async def test_litellm_adapter_backward_compatibility(monkeypatch: pytest.MonkeyPatch):
    """测试：适配器保持对现有纯文本响应的向后兼容性。

    验证旧的测试场景仍然通过，确保没有破坏现有功能。
    """
    adapter = LiteLLMAdapter(timeout_seconds=60)  # 创建适配器实例，测试真实路径

    # 使用旧的 FakeLiteLLMResponse 类（没有 tool_calls 属性）
    fake_chunks = [  # 构造模拟 chunk 列表
        FakeLiteLLMResponse("Hello"),
        FakeLiteLLMResponse(" "),
        FakeLiteLLMResponse("world"),
    ]
    install_fake_litellm(  # 安装假的 litellm 模块
        monkeypatch,
        response_stream=build_fake_stream(fake_chunks),
    )

    # 收集归一化后的 chunks
    chunks = []  # 初始化 chunk 列表
    async for chunk in adapter.stream_completion(  # 调用适配器流式方法
        model="gpt-4.1-mini",
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.2,
        api_key="fake-key",
    ):
        chunks.append(chunk)  # 收集 chunk

    # 验证结果与旧测试一致
    assert len(chunks) == 3
    assert chunks[0].content == "Hello"
    assert chunks[1].content == " "
    assert chunks[2].content == "world"
    # 验证新字段为 None
    assert all(c.tool_calls is None for c in chunks)
    assert all(c.finish_reason is None for c in chunks)


@pytest.mark.asyncio
async def test_litellm_adapter_counts_prompt_tokens(monkeypatch: pytest.MonkeyPatch):
    """测试：适配器会通过 LiteLLM token_counter 统计输入 token。"""
    adapter = LiteLLMAdapter(timeout_seconds=60)
    fake_litellm = install_fake_litellm(
        monkeypatch,
        response_stream=build_fake_stream([]),
    )
    fake_litellm.token_counter = lambda **kwargs: 321

    token_count = await adapter.count_prompt_tokens(
        model="gpt-4.1-mini",
        messages=[{"role": "user", "content": "hello"}],
    )

    assert token_count == 321
    assert fake_litellm.request_timeout == 60


@pytest.mark.asyncio
async def test_litellm_adapter_complete_text_extracts_non_stream_response(monkeypatch: pytest.MonkeyPatch):
    """测试：适配器能从非流式补全响应中提取最终文本。"""
    adapter = LiteLLMAdapter(timeout_seconds=60)

    async def fake_acompletion(**kwargs):
        del kwargs
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="压缩后的摘要正文"),
                )
            ]
        )

    fake_module = SimpleNamespace(
        request_timeout=None,
        acompletion=fake_acompletion,
        token_counter=lambda **kwargs: 0,
    )
    monkeypatch.setitem(sys.modules, "litellm", fake_module)

    content = await adapter.complete_text(
        model="gpt-4.1-mini",
        messages=[{"role": "user", "content": "请压缩"}],
        temperature=0.0,
    )

    assert content == "压缩后的摘要正文"
    assert fake_module.request_timeout == 60


@pytest.mark.asyncio
async def test_litellm_adapter_complete_text_disables_deepseek_thinking_by_default(monkeypatch: pytest.MonkeyPatch):
    """测试：DeepSeek 非流式摘要调用默认显式关闭 thinking。"""
    adapter = LiteLLMAdapter(timeout_seconds=60)

    fake_module = SimpleNamespace(
        request_timeout=None,
        acompletion=None,
        token_counter=lambda **kwargs: 0,
        last_acompletion_kwargs=None,
    )

    async def fake_acompletion(**kwargs):
        fake_module.last_acompletion_kwargs = kwargs
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="摘要"))]
        )

    fake_module.acompletion = fake_acompletion
    monkeypatch.setitem(sys.modules, "litellm", fake_module)

    content = await adapter.complete_text(
        model="deepseek/deepseek-v4-flash",
        messages=[{"role": "user", "content": "请压缩"}],
        temperature=0.0,
    )

    assert content == "摘要"
    assert fake_module.last_acompletion_kwargs is not None
    assert fake_module.last_acompletion_kwargs["extra_body"] == {
        "thinking": {"type": "disabled"},
    }

