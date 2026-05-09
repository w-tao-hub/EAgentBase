"""测试用的假实现（Fake Implementations）。

提供服务层和 HTTP 层测试共用的假 runtime 和 fake adapter。
"""

from __future__ import annotations

from typing import AsyncIterator, Any
from datetime import datetime, timezone

from app.core.models.agent import Agent
from app.core.models.run import Run, RunStatus
from app.core.models.stored_message import StoredMessage
from app.core.models.event import (
    RunStartedEvent,
    MessageDeltaEvent,
    RunCompletedEvent,
    RunFailedEvent,
    ToolUseStartedEvent,
    ToolUseCompletedEvent,
)
from app.core.models.error import ErrorCode
from app.core.models.tool import Tool, ToolResult
from app.core.models.execution_context import ExecutionContext
from app.core.models.llm_chunk import LLMChunk
from app.core.runtime.agent_runtime import TurnComplete


class FakeLLMChunk(LLMChunk):
    """模拟 LLM 返回的 chunk 对象。"""


class FakeLLMAdapter:
    """模拟 LLM 适配器。

    用于测试时替代真实的 LiteLLM 适配器。
    """

    def __init__(
        self,
        chunks: list[str | LLMChunk] | None = None,
        turn_chunks: list[list[str | LLMChunk]] | None = None,
        prompt_tokens: int = 0,
        prompt_token_counts: list[int] | None = None,
        completion_text: str = "",
        completion_errors: list[Exception] | None = None,
        raise_error: bool = False,
    ) -> None:
        """初始化模拟 LLM 适配器。"""
        self.chunks = chunks or []
        self.turn_chunks = turn_chunks or []
        self.prompt_tokens = prompt_tokens
        self.prompt_token_counts = list(prompt_token_counts or [])
        self.completion_text = completion_text
        self.completion_errors = list(completion_errors or [])
        self.raise_error = raise_error
        self.last_call: dict[str, Any] | None = None
        self.last_completion_call: dict[str, Any] | None = None
        self.call_count = 0

    async def count_prompt_tokens(self, model: str, messages: list[dict[str, Any]]) -> int:
        """模拟输入 token 统计接口。"""
        self.last_call = {
            "model": model,
            "messages": messages,
            "kind": "count_prompt_tokens",
        }
        if self.prompt_token_counts:
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
        self.last_completion_call = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "api_key": api_key,
            "enable_thinking": enable_thinking,
            "reasoning_effort": reasoning_effort,
        }
        if self.completion_errors:
            raise self.completion_errors.pop(0)
        return self.completion_text

    async def stream_completion(
        self,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        api_key: str | None = None,
        tools: list[dict] | None = None,
        reasoning_effort: str | None = None,
    ) -> AsyncIterator[FakeLLMChunk]:
        """模拟流式完成调用。

        Yields:
            FakeLLMChunk 对象

        Raises:
            Exception: 如果设置了 raise_error=True
        """
        self.last_call = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "api_key": api_key,
            "tools": tools,
            "reasoning_effort": reasoning_effort,
        }
        self.call_count += 1

        if self.raise_error:
            raise Exception("Fake LLM error")

        current_chunks = self.chunks
        if self.turn_chunks:
            current_chunks = self.turn_chunks[min(self.call_count - 1, len(self.turn_chunks) - 1)]

        for chunk in current_chunks:
            if isinstance(chunk, LLMChunk):
                yield FakeLLMChunk(
                    content=chunk.content,
                    thinking=chunk.thinking,
                    tool_calls=chunk.tool_calls,
                    finish_reason=chunk.finish_reason,
                    usage=chunk.usage,
                )
            else:
                yield FakeLLMChunk(content=chunk)


class FakeTool(Tool):
    """模拟工具实现。

    用于测试时替代真实的工具实现，可以预设返回的 ToolResult。
    """

    def __init__(
        self,
        name: str,
        description: str,
        input_schema: dict,
        return_value: ToolResult,
    ) -> None:
        """初始化模拟工具。"""
        self._name = name
        self._description = description
        self._input_schema = input_schema
        self._return_value = return_value

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
        events: list[Any] | None = None,
        raise_error: bool = False,
        turn_results: list[list[Any]] | None = None,
    ) -> None:
        """初始化模拟 AgentRuntime。"""
        self.events = events or []
        self.raise_error = raise_error
        self.turn_results = turn_results or []
        self.last_call: dict[str, Any] | None = None
        self._turn_count = 0

    async def stream(
        self,
        agent: Agent,
        run: Run,
        messages: list[dict[str, str]],
    ) -> AsyncIterator[Any]:
        """模拟流式执行。

        Yields:
            预设的事件对象（run_id 会被替换为传入 run 的 run_id）

        Raises:
            Exception: 如果设置了 raise_error=True
        """
        self.last_call = {
            "agent": agent,
            "run": run,
            "messages": messages,
        }

        if self.raise_error:
            raise Exception("Fake runtime error")

        for event in self.events:
            if hasattr(event, "run_id"):
                event_data = event.model_dump()
                event_data["run_id"] = run.run_id
                from app.core.models.event import (
                    RunStartedEvent,
                    MessageDeltaEvent,
                    RunCompletedEvent,
                    RunFailedEvent,
                )
                if isinstance(event, RunStartedEvent):
                    yield RunStartedEvent(**event_data)
                elif isinstance(event, MessageDeltaEvent):
                    yield MessageDeltaEvent(**event_data)
                elif isinstance(event, RunCompletedEvent):
                    yield RunCompletedEvent(**event_data)
                elif isinstance(event, RunFailedEvent):
                    yield RunFailedEvent(**event_data)
                else:
                    yield event
            else:
                yield event

    async def stream_once(
        self,
        agent: Agent,
        messages: list[dict[str, str]],
        tools: list[dict] | None = None,
        context: ExecutionContext | None = None,
    ) -> AsyncIterator[str | TurnComplete]:
        """模拟单次流式调用，返回 str 和 TurnComplete。

        支持两种工作模式：
        1. turn_results 模式：按轮次直接返回预设结果，支持 tool_calls 和多轮。
        2. events 兼容模式：从 events 列表解析 MessageDeltaEvent / RunFailedEvent / RunCompletedEvent。

        Yields:
            str: 流式文本片段
            TurnComplete: 最终完成标记

        Raises:
            Exception: 如果设置了 raise_error=True 或 events 中包含 RunFailedEvent
        """
        self.last_call = {
            "agent": agent,
            "messages": messages,
            "tools": tools,
            "context": context,
        }

        if self.raise_error:
            raise Exception("Fake runtime error")

        # 模式 1：使用 turn_results 按轮次返回
        if self.turn_results:
            chunks = self.turn_results[min(self._turn_count, len(self.turn_results) - 1)]
            self._turn_count += 1
            for item in chunks:
                yield item
            return

        # 模式 2：从 events 提取内容（向后兼容）
        full_text = ""
        output_text = ""
        has_failed = False
        failed_message = ""

        for event in self.events:
            if isinstance(event, MessageDeltaEvent):
                if event.content:
                    full_text += event.content
                    yield event.content
            elif isinstance(event, RunCompletedEvent):
                output_text = event.output
            elif isinstance(event, RunFailedEvent):
                has_failed = True
                failed_message = event.message

        if has_failed:
            raise Exception(failed_message or "Fake runtime error")

        yield TurnComplete(
            tool_calls=None,
            usage=None,
        )


def create_fake_agent() -> Agent:
    """创建一个用于测试的模拟 Agent。"""
    return Agent(
        agent_id="test-agent",
        name="Test Agent",
        model="gpt-4.1-mini",
        system_prompt="You are a helpful assistant.",
        temperature=0.2,
    )


def create_fake_run(
    run_id: str = "run-1",
    session_id: str = "session-1",
    status: RunStatus = RunStatus.RUNNING,
) -> Run:
    """创建一个用于测试的模拟 Run。"""
    return Run(
        run_id=run_id,
        session_id=session_id,
        status=status,
        created_at=datetime.now(timezone.utc),
    )


def create_fake_message(
    role: str = "user",
    content: str = "Hello",
) -> StoredMessage:
    """创建一个用于测试的模拟 StoredMessage。"""
    return StoredMessage.create(
        role=role,
        content=content,
        timestamp=datetime.now(timezone.utc),
    )
