"""测试用的假实现（Fake Implementations）。

提供服务层和 HTTP 层测试共用的假 runtime 和 fake adapter。
"""

from __future__ import annotations  # 启用未来注解

from typing import AsyncIterator, Any  # 导入异步迭代器和任意类型
from datetime import datetime, timezone  # 导入日期时间类和 UTC 时区

from app.core.models.agent import Agent  # 导入 Agent 模型
from app.core.models.run import Run, RunStatus  # 导入 Run 模型和状态枚举
from app.core.models.stored_message import StoredMessage  # 导入 StoredMessage 模型
from app.core.models.event import (  # 导入事件模型
    RunStartedEvent,
    MessageDeltaEvent,
    RunCompletedEvent,
    RunFailedEvent,
    ToolUseStartedEvent,
    ToolUseCompletedEvent,
)
from app.core.models.error import ErrorCode  # 导入错误码枚举
from app.core.models.tool import Tool, ToolResult  # 导入工具抽象基类和结果模型
from app.core.models.execution_context import ExecutionContext  # 导入执行上下文模型
from app.core.models.llm_chunk import LLMChunk  # 导入统一 chunk 模型，供 fake adapter 直接复用
from app.core.runtime.agent_runtime import TurnComplete  # 导入流式响应模型


class FakeLLMChunk(LLMChunk):
    """模拟 LLM 返回的 chunk 对象。"""


class FakeLLMAdapter:
    """模拟 LLM 适配器。

    用于测试时替代真实的 LiteLLM 适配器。
    """

    def __init__(
        self,
        chunks: list[str | LLMChunk] | None = None,  # 要返回的单轮 chunks
        turn_chunks: list[list[str | LLMChunk]] | None = None,  # 每次调用要返回的 chunks 列表
        prompt_tokens: int = 0,  # 输入 token 统计返回值
        prompt_token_counts: list[int] | None = None,  # 输入 token 统计按调用顺序返回的数列
        completion_text: str = "",  # 非流式补全默认返回值
        completion_errors: list[Exception] | None = None,  # 非流式补全按调用顺序抛出的异常列表
        raise_error: bool = False,  # 是否抛出异常
    ) -> None:  # 构造函数
        """初始化模拟 LLM 适配器。

        Args:
            chunks: 每次调用都重复返回的 chunk 列表
            turn_chunks: 按调用轮次返回的 chunk 列表，适合多轮工具调用测试
            raise_error: 是否在调用时抛出异常
        """
        self.chunks = chunks or []  # 保存 chunks 列表
        self.turn_chunks = turn_chunks or []  # 保存按轮次区分的 chunks 列表
        self.prompt_tokens = prompt_tokens  # 保存输入 token 统计返回值
        self.prompt_token_counts = list(prompt_token_counts or [])  # 保存输入 token 统计数列
        self.completion_text = completion_text  # 保存非流式补全返回值
        self.completion_errors = list(completion_errors or [])  # 保存非流式补全异常序列
        self.raise_error = raise_error  # 保存是否抛出异常
        self.last_call: dict[str, Any] | None = None  # 记录最后一次调用参数
        self.last_completion_call: dict[str, Any] | None = None  # 记录最后一次非流式补全参数
        self.call_count = 0  # 记录调用次数，便于多轮场景返回不同 chunk

    async def count_prompt_tokens(self, model: str, messages: list[dict[str, Any]]) -> int:
        """模拟输入 token 统计接口。"""
        self.last_call = {  # 复用 last_call 记录最近一次计数参数，便于测试断言。
            "model": model,
            "messages": messages,
            "kind": "count_prompt_tokens",
        }
        if self.prompt_token_counts:  # 配置了按调用顺序返回的计数值时，优先消费该序列。
            return self.prompt_token_counts.pop(0)
        return self.prompt_tokens

    async def complete_text(
        self,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float,
        api_key: str | None = None,
        enable_thinking: bool = False,
        reasoning_effort: str | None = None,
    ) -> str:
        """模拟非流式补全接口。"""
        self.last_completion_call = {  # 记录最近一次摘要调用参数，便于测试断言。
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "api_key": api_key,
            "enable_thinking": enable_thinking,
            "reasoning_effort": reasoning_effort,
        }
        if self.completion_errors:  # 配置了异常序列时，按顺序抛出首个异常。
            raise self.completion_errors.pop(0)
        return self.completion_text

    async def stream_completion(
        self,
        model: str,  # 模型名称
        messages: list[dict[str, str]],  # 消息列表
        temperature: float,  # 温度参数
        api_key: str | None = None,  # API 密钥
        tools: list[dict] | None = None,  # 工具列表
        reasoning_effort: str | None = None,  # thinking 模式的思考强度
    ) -> AsyncIterator[FakeLLMChunk]:  # 返回异步迭代器
        """模拟流式完成调用。

        Args:
            model: 模型名称
            messages: 消息列表
            temperature: 温度参数
            api_key: API 密钥
            tools: 工具定义列表，本替身只记录不消费

        Yields:
            FakeLLMChunk 对象

        Raises:
            Exception: 如果设置了 raise_error=True
        """
        # 记录调用参数
        self.last_call = {  # 记录最后一次调用
            "model": model,  # 模型名称
            "messages": messages,  # 消息列表
            "temperature": temperature,  # 温度参数
            "api_key": api_key,  # API 密钥
            "tools": tools,  # 工具列表
            "reasoning_effort": reasoning_effort,  # DeepSeek thinking 模式的思考强度
        }
        self.call_count += 1  # 记录调用次数，供 turn_chunks 模式索引

        if self.raise_error:  # 如果设置了抛出异常
            raise Exception("Fake LLM error")  # 抛出异常

        # 优先使用按轮次区分的 chunks，适配工具调用等多轮流式场景。
        current_chunks = self.chunks  # 默认使用单轮复用配置
        if self.turn_chunks:  # 如果配置了按轮次区分的 chunks
            current_chunks = self.turn_chunks[min(self.call_count - 1, len(self.turn_chunks) - 1)]  # 读取当前轮的 chunk 列表

        # 依次返回每个 chunk
        for chunk in current_chunks:  # 遍历 chunks
            if isinstance(chunk, LLMChunk):  # 如果调用方已提供完整 chunk，对应真实链路直接透传
                yield FakeLLMChunk(  # 复制为 fake chunk，保持测试语义统一
                    content=chunk.content,
                    thinking=chunk.thinking,
                    tool_calls=chunk.tool_calls,
                    finish_reason=chunk.finish_reason,
                    usage=chunk.usage,
                )
            else:  # 否则按纯文本快捷写法处理
                yield FakeLLMChunk(content=chunk)  # 生成只含文本的 chunk


class FakeTool(Tool):
    """模拟工具实现。

    用于测试时替代真实的工具实现，可以预设返回的 ToolResult。
    """

    def __init__(
        self,
        name: str,  # 工具名称
        description: str,  # 工具描述
        input_schema: dict,  # 输入参数 schema
        return_value: ToolResult,  # 预设的返回值
    ) -> None:
        """初始化模拟工具。

        Args:
            name: 工具标识符
            description: 工具描述
            input_schema: JSON Schema 格式的输入参数定义
            return_value: 调用时返回的预设结果
        """
        self._name = name  # 保存工具名称
        self._description = description  # 保存工具描述
        self._input_schema = input_schema  # 保存输入 schema
        self._return_value = return_value  # 保存预设返回值

    @property
    def name(self) -> str:
        """返回工具名称。"""
        return self._name

    @property
    def description(self) -> str:
        """返回工具描述。"""
        return self._description

    @property
    def input_schema(self) -> dict:
        """返回输入参数 schema。"""
        return self._input_schema

    def is_read_only(self) -> bool:
        """返回是否为只读工具，模拟工具默认返回 True。"""
        return True

    async def call(self, input: dict, context: ExecutionContext) -> ToolResult:
        """模拟工具调用，返回预设的 ToolResult。

        Args:
            input: 工具输入参数（会被记录但不影响返回值）
            context: 执行上下文，本测试替身不消费该参数

        Returns:
            预设的 ToolResult 实例
        """
        return self._return_value


class FakeAgentRuntime:
    """模拟 AgentRuntime。

    用于测试时替代真实的 AgentRuntime。
    可以预设要返回的事件序列，或通过 turn_results 精确控制 stream_once 行为。
    """

    def __init__(
        self,
        events: list[Any] | None = None,  # 要返回的事件列表（用于旧 stream 接口和单轮 stream_once）
        raise_error: bool = False,  # 是否抛出异常
        turn_results: list[list[Any]] | None = None,  # 每轮 stream_once 要返回的片段/结果列表
    ) -> None:  # 构造函数
        """初始化模拟 AgentRuntime。

        Args:
            events: 要返回的事件列表
            raise_error: 是否在调用时抛出异常
            turn_results: 多轮次 stream_once 结果预设，每轮为一个列表
        """
        self.events = events or []  # 保存事件列表
        self.raise_error = raise_error  # 保存是否抛出异常
        self.turn_results = turn_results or []  # 保存多轮次结果列表
        self.last_call: dict[str, Any] | None = None  # 记录最后一次调用参数
        self._turn_count = 0  # 记录 stream_once 调用轮次

    async def stream(
        self,
        agent: Agent,  # Agent 配置
        run: Run,  # Run 实例
        messages: list[dict[str, str]],  # 已准备好的 LLM 消息列表
    ) -> AsyncIterator[Any]:  # 返回异步迭代器
        """模拟流式执行。

        Args:
            agent: Agent 配置
            run: Run 实例
            messages: 已准备好的 LLM 消息列表

        Yields:
            预设的事件对象（run_id 会被替换为传入 run 的 run_id）

        Raises:
            Exception: 如果设置了 raise_error=True
        """
        # 记录调用参数
        self.last_call = {  # 记录最后一次调用
            "agent": agent,  # Agent 配置
            "run": run,  # Run 实例
            "messages": messages,  # 消息列表
        }

        if self.raise_error:  # 如果设置了抛出异常
            raise Exception("Fake runtime error")  # 抛出异常

        # 依次返回每个事件，使用传入 run 的 run_id 替换事件中的 run_id
        for event in self.events:  # 遍历事件列表
            # 动态替换事件中的 run_id 为传入 run 的 run_id
            if hasattr(event, "run_id"):  # 检查事件是否有 run_id 属性
                # 创建新的事件实例，使用正确的 run_id
                event_data = event.model_dump()  # 序列化为字典
                event_data["run_id"] = run.run_id  # 替换 run_id
                # 根据事件类型重建事件对象
                from app.core.models.event import (
                    RunStartedEvent,
                    MessageDeltaEvent,
                    RunCompletedEvent,
                    RunFailedEvent,
                )
                if isinstance(event, RunStartedEvent):
                    yield RunStartedEvent(**event_data)  # 生成新的 RunStartedEvent
                elif isinstance(event, MessageDeltaEvent):
                    yield MessageDeltaEvent(**event_data)  # 生成新的 MessageDeltaEvent
                elif isinstance(event, RunCompletedEvent):
                    yield RunCompletedEvent(**event_data)  # 生成新的 RunCompletedEvent
                elif isinstance(event, RunFailedEvent):
                    yield RunFailedEvent(**event_data)  # 生成新的 RunFailedEvent
                else:
                    yield event  # 未知事件类型，原样返回
            else:
                yield event  # 没有 run_id 的事件，原样返回

    async def stream_once(
        self,
        agent: Agent,  # Agent 配置
        messages: list[dict[str, str]],  # 已准备好的 LLM 消息列表
        tools: list[dict] | None = None,  # 可选的工具列表
        context: ExecutionContext | None = None,  # 可选的执行上下文
    ) -> AsyncIterator[str | TurnComplete]:
        """模拟单次流式调用，返回 str 和 TurnComplete。

        支持两种工作模式：
        1. turn_results 模式：按轮次直接返回预设结果，支持 tool_calls 和多轮。
        2. events 兼容模式：从 events 列表解析 MessageDeltaEvent / RunFailedEvent / RunCompletedEvent。

        Args:
            agent: Agent 配置
            messages: 已准备好的 LLM 消息列表
            tools: 可选的工具列表
            context: 执行上下文，供新 Hook 链测试使用

        Yields:
            str: 流式文本片段
            TurnComplete: 最终完成标记

        Raises:
            Exception: 如果设置了 raise_error=True 或 events 中包含 RunFailedEvent
        """
        # 记录调用参数
        self.last_call = {  # 记录最后一次调用
            "agent": agent,  # Agent 配置
            "messages": messages,  # 消息列表
            "tools": tools,  # 工具列表
            "context": context,  # 执行上下文
        }

        if self.raise_error:  # 如果设置了抛出异常
            raise Exception("Fake runtime error")  # 抛出异常

        # 模式 1：使用 turn_results 按轮次返回
        if self.turn_results:  # 如果配置了多轮次结果
            chunks = self.turn_results[min(self._turn_count, len(self.turn_results) - 1)]
            self._turn_count += 1
            for item in chunks:
                yield item
            return

        # 模式 2：从 events 提取内容（向后兼容）
        full_text = ""  # 初始化完整文本
        output_text = ""  # 如果有 RunCompletedEvent，使用其 output
        has_failed = False  # 是否包含失败事件
        failed_message = ""  # 失败消息

        for event in self.events:  # 遍历事件列表
            if isinstance(event, MessageDeltaEvent):  # 如果是消息增量事件
                if event.content:  # 内容不为空才 yield
                    full_text += event.content  # 累积文本
                    yield event.content  # 生成 str 文本片段
            elif isinstance(event, RunCompletedEvent):  # 运行完成事件
                output_text = event.output  # 记录输出
            elif isinstance(event, RunFailedEvent):  # 运行失败事件
                has_failed = True  # 标记失败
                failed_message = event.message  # 记录失败消息

        if has_failed:  # 如果包含失败事件，抛出异常
            raise Exception(failed_message or "Fake runtime error")

        # 最后生成 TurnComplete
        yield TurnComplete(
            tool_calls=None,  # events 模式暂不支持工具调用
            usage=None,  # Token 用量信息
        )


def create_fake_agent() -> Agent:  # 创建模拟 Agent
    """创建一个用于测试的模拟 Agent。

    Returns:
        Agent 实例
    """
    return Agent(  # 构造 Agent 实例
        agent_id="test-agent",  # Agent ID
        name="Test Agent",  # Agent 名称
        model="gpt-4.1-mini",  # 模型名称
        system_prompt="You are a helpful assistant.",  # 系统提示词
        temperature=0.2,  # 温度参数
    )


def create_fake_run(
    run_id: str = "run-1",  # Run ID
    session_id: str = "session-1",  # Session ID
    status: RunStatus = RunStatus.RUNNING,  # 状态
) -> Run:  # 创建模拟 Run
    """创建一个用于测试的模拟 Run。

    Args:
        run_id: Run ID
        session_id: Session ID
        status: Run 状态

    Returns:
        Run 实例
    """
    return Run(  # 构造 Run 实例
        run_id=run_id,  # Run ID
        session_id=session_id,  # Session ID
        status=status,  # 状态
        created_at=datetime.now(timezone.utc),  # 创建时间
    )


def create_fake_message(
    role: str = "user",  # 角色
    content: str = "Hello",  # 内容
) -> StoredMessage:  # 创建模拟消息
    """创建一个用于测试的模拟 StoredMessage。

    Args:
        role: 消息角色
        content: 消息内容

    Returns:
        StoredMessage 实例
    """
    return StoredMessage.create(  # 构造 StoredMessage 实例
        role=role,  # 角色
        content=content,  # 内容
        timestamp=datetime.now(timezone.utc),  # 时间戳
    )
