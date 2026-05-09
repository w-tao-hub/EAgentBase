"""AgentRuntime 实现。

提供单次 LLM 调用的流式执行语义，职责收窄为单次调用。
不再发射 Event，改为 yield str 和 TurnComplete。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import AsyncIterator, TYPE_CHECKING
import uuid
from app.core.hooks import (
    ModelHookPipeline,
    ModelRequest,
    ModelResponse,
    NoOpStreamTextGuard,
    StreamTextGuard,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.core.models.execution_context import ExecutionContext


@dataclass
class UsageInfo:
    """Token 用量信息。

    封装单次 LLM 调用的 token 消耗统计。
    """

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass
class ToolCall:
    """工具调用定义。

    表示 LLM 请求调用一个工具，采用 OpenAI 兼容的嵌套格式。
    """

    id: str
    type: str
    function: Function


@dataclass
class Function:
    """函数调用信息。

    嵌套在 ToolCall 内，包含函数名和参数。
    """

    name: str
    arguments: str


@dataclass
class TurnComplete:
    """单次 LLM 调用完成标记。

    在流式输出的最后 yield，携带工具调用列表和 Token 用量信息。
    不包含文本内容，文本内容由调用方通过累加 yield 的 str 获得。
    """

    tool_calls: list[ToolCall] | None = None
    usage: UsageInfo | None = None
    reasoning_content: str | None = None


class AgentRuntime:
    """Agent 运行时。

    职责收窄为单次 LLM 调用，不再负责事件流管理。
    主要功能：
    1. 流式调用 LLM，yield str（文本片段）
    2. 累积 tool_call 碎片
    3. 提取 Token 用量信息
    4. 最后 yield TurnComplete（完成标记）
    5. 瞬态错误重试（1-2 次）
    """

    def __init__(
        self,
        llm_adapter: object,
        model_hook_pipeline: ModelHookPipeline | None = None,
        stream_text_guard: StreamTextGuard | None = None,
    ) -> None:
        """初始化 AgentRuntime。

        Args:
            llm_adapter: LLM 适配器实例，必须实现 stream_completion 方法
            model_hook_pipeline: 模型 Hook 串行执行管线，未提供时使用空管线
            stream_text_guard: 流式文本守卫，未提供时使用 no-op 守卫
        """
        self._llm_adapter = llm_adapter
        self._model_hook_pipeline = model_hook_pipeline or ModelHookPipeline()
        self._stream_text_guard = stream_text_guard or NoOpStreamTextGuard()
        self._max_retries = 2

    async def stream_once(
        self,
        agent: object,
        messages: list[dict],
        tools: list[dict] | None = None,
        context: "ExecutionContext | None" = None,
    ) -> AsyncIterator[str | TurnComplete]:
        """执行单次 LLM 调用，流式返回 str，最后返回 TurnComplete。

        Args:
            agent: Agent 配置，包含 model、temperature 等属性
            messages: 已经准备完成的 LLM 消息列表
            tools: 可选的工具列表，用于工具调用场景
            context: 执行上下文，未提供时表示当前调用不使用 Hook 扩展语义

        Yields:
            str: 流式文本片段
            TurnComplete: 最终完成标记，包含 tool_calls 和 usage

        Raises:
            Exception: 当重试次数耗尽后仍然失败时抛出异常
        """
        last_error = None

        for attempt in range(self._max_retries):
            try:
                async for item in self._do_stream_once(agent, messages, tools, context):
                    yield item
                return
            except Exception as e:
                last_error = e
                error_msg = str(e).lower()

                if isinstance(e, asyncio.CancelledError):
                    raise

                is_transient = any(
                    keyword in error_msg
                    for keyword in [
                        "connection",
                        "timeout",
                        "reset",
                        "refused",
                        "temporarily",
                        "unavailable",
                        "rate limit",
                        "503",
                        "502",
                        "504",
                    ]
                )

                if is_transient and attempt < self._max_retries - 1:
                    logger.warning(
                        "LLM 调用遇到瞬态错误，准备重试: attempt=%d, error=%s",
                        attempt + 1,
                        e,
                    )
                    continue
                else:
                    raise

        if last_error:
            raise last_error

    async def _do_stream_once(
        self,
        agent: object,
        messages: list[dict],
        tools: list[dict] | None = None,
        context: "ExecutionContext | None" = None,
    ) -> AsyncIterator[str | TurnComplete]:
        """实际执行单次 LLM 调用的内部方法。

        Args:
            agent: Agent 配置
            messages: LLM 消息列表
            tools: 可选的工具列表

        Yields:
            str: 流式文本片段
            TurnComplete: 最终完成标记
        """
        # # 初始化累积变量
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        # 工具调用累积器，key 使用 tool_call 的 index。后续片段往往只携带 index，
        # 不会重复携带 id / function_name，因此必须按 index 归并，不能只按 id 归并。
        tool_call_accumulator: dict[int, dict] = {}
        usage_info: UsageInfo | None = None

        # 构造模型请求对象，并通过 before_model Hook 串行改写。
        model_request = ModelRequest(
            messages=list(messages),
            tools=list(tools) if tools is not None else None,
            model=agent.model,
            temperature=agent.temperature,
        )
        if context is not None:
            model_request = await self._model_hook_pipeline.before_model(model_request, context)

        async for chunk in self._llm_adapter.stream_completion(
            model=model_request.model,
            messages=model_request.messages,
            temperature=model_request.temperature,
            api_key=None,
            tools=model_request.tools,
            reasoning_effort=agent.reasoning_effort,
        ):
            # 若取消信号已设置，主动抛 CancelledError 中断流式读取
            if context is not None and context.cancel_event.is_set():
                raise asyncio.CancelledError("Run cancelled during streaming")

            thinking = self._extract_thinking(chunk)
            if thinking:
                reasoning_parts.append(thinking)

            content = self._extract_content(chunk)
            if content:
                guarded_chunks = [content]
                if context is not None:
                    guarded_chunks = await self._stream_text_guard.ingest_text(content, context)
                for guarded_chunk in guarded_chunks:
                    text_parts.append(guarded_chunk)
                    yield guarded_chunk

            tool_call_deltas = self._extract_tool_calls(chunk)
            for tc_delta in tool_call_deltas:
                tc_id = tc_delta.get("id")
                tc_index = tc_delta.get("index", 0)

                if tc_index not in tool_call_accumulator:
                    tool_call_accumulator[tc_index] = {
                        "id": tc_id or "",
                        "name": "",
                        "arguments": "",
                    }

                # 后续增量片段可能补上 id，在已有累积器上持续回填
                if tc_id:
                    tool_call_accumulator[tc_index]["id"] = tc_id

                if tc_delta.get("function_name"):
                    tool_call_accumulator[tc_index]["name"] = tc_delta["function_name"]
                elif tc_delta.get("name"):
                    tool_call_accumulator[tc_index]["name"] = tc_delta["name"]

                if tc_delta.get("arguments"):
                    tool_call_accumulator[tc_index]["arguments"] += tc_delta["arguments"]

            chunk_usage = self._extract_usage(chunk)
            if chunk_usage:
                usage_info = chunk_usage

        if context is not None:
            for guarded_chunk in await self._stream_text_guard.flush(context):
                text_parts.append(guarded_chunk)
                yield guarded_chunk

        tool_calls: list[ToolCall] | None = None
        if tool_call_accumulator:
            tool_calls = [
                ToolCall(
                    id=(
                        tc["id"]
                        if tc["id"]
                        else (
                            logger.warning(f"工具调用片段缺少 id,已生成临时 id: index={str(uuid.uuid4())}"),
                            f"auto-generated-call-{str(uuid.uuid4())}",
                        )[1]
                    ),
                    type="function",
                    function=Function(
                        name=tc["name"],
                        arguments=tc["arguments"],
                    ),
                )
                for _, tc in sorted(tool_call_accumulator.items())
            ]

        model_response = ModelResponse(
            text="".join(text_parts),
            tool_calls=tool_calls,
            usage=usage_info,
        )
        if context is not None:
            model_response = await self._model_hook_pipeline.after_model(model_response, context)

        # after_model 若改写 text，不会回放已流出的 chunk；当前仅对 tool_calls / usage 生效
        yield TurnComplete(
            tool_calls=model_response.tool_calls,
            usage=model_response.usage,
            reasoning_content="".join(reasoning_parts) if reasoning_parts else None,
        )

    def _extract_content(self, chunk: object) -> str | None:
        """从 LLMChunk 中提取文本内容。

        Args:
            chunk: LLMChunk 对象

        Returns:
            提取的文本内容，如果不存在则返回 None
        """
        return chunk.content if hasattr(chunk, "content") else None

    def _extract_tool_calls(self, chunk: object) -> list[dict]:
        """从 LLMChunk 中提取工具调用增量。

        Args:
            chunk: LLMChunk 对象

        Returns:
            工具调用增量列表，每个元素包含 index、id、function_name、arguments
        """
        # LLMChunk 直接提供 tool_calls 属性
        tool_calls_attr = getattr(chunk, "tool_calls", None)
        if tool_calls_attr is None:
            return []
        return tool_calls_attr if isinstance(tool_calls_attr, list) else []

    def _extract_thinking(self, chunk: object) -> str | None:
        """从 LLMChunk 中提取 reasoning / thinking 文本。

        Args:
            chunk: LLMChunk 对象

        Returns:
            当前 chunk 携带的思考内容；不存在时返回 None
        """
        return chunk.thinking if hasattr(chunk, "thinking") else None

    def _extract_usage(self, chunk: object) -> UsageInfo | None:
        """从 LLMChunk 中提取 Token 用量信息。

        Args:
            chunk: LLMChunk 对象

        Returns:
            UsageInfo 对象，如果不存在则返回 None
        """
        return chunk.usage if hasattr(chunk, "usage") else None
