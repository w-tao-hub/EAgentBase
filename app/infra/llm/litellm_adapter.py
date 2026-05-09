"""LiteLLM 适配器实现。

提供 LiteLLM 的流式调用适配，将 chunk 归一化为统一格式。
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

from app.core.models.llm_chunk import LLMChunk
from app.core.runtime.agent_runtime import UsageInfo
from app.infra.logging import get_logger

logger = get_logger(__name__)


class LiteLLMAdapter:
    """将 LiteLLM 流式响应归一化为统一 LLMChunk 格式。"""

    def __init__(self, timeout_seconds: int = 60) -> None:
        self._timeout_seconds = timeout_seconds

    async def count_prompt_tokens(self, model: str, messages: list[dict[str, Any]]) -> int:
        """统计待发送消息的输入 token 数。"""
        litellm = self._load_litellm()
        litellm.request_timeout = self._timeout_seconds
        return int(await asyncio.to_thread(litellm.token_counter, model=model, messages=messages))

    async def complete_text(
        self,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float,
        api_key: str | None = None,
        enable_thinking: bool = False,
        reasoning_effort: str | None = None,
    ) -> str:
        """执行非流式文本补全。"""
        litellm = self._load_litellm()
        litellm.request_timeout = self._timeout_seconds
        completion_kwargs = self._build_completion_kwargs(
            model=model,
            messages=messages,
            temperature=temperature,
            api_key=api_key,
            tools=None,
            enable_thinking=enable_thinking,
            reasoning_effort=reasoning_effort,
        )

        response = await litellm.acompletion(**completion_kwargs, stream=False)
        content = self._extract_completion_text(response)
        if content is None:
            raise ValueError("LiteLLM 非流式补全未返回可用文本")
        return content

    async def stream_completion(
        self,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        api_key: str | None = None,
        tools: list[dict] | None = None,
        reasoning_effort: str | None = None,
    ) -> AsyncIterator[LLMChunk]:
        """调用 LiteLLM 进行流式对话完成。"""
        litellm = self._load_litellm()
        litellm.request_timeout = self._timeout_seconds

        completion_kwargs = self._build_completion_kwargs(
            model=model,
            messages=messages,
            temperature=temperature,
            api_key=api_key,
            tools=tools,
            enable_thinking=True,
            reasoning_effort=reasoning_effort,
        )

        response_stream = await litellm.acompletion(**completion_kwargs, stream=True)
        async for chunk in response_stream:
            normalized = self._normalize_single_chunk(chunk)
            if normalized is not None:
                yield normalized

    @staticmethod
    def _load_litellm() -> Any:
        """延迟导入 LiteLLM。"""
        import litellm
        return litellm

    def _build_completion_kwargs(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float,
        api_key: str | None,
        tools: list[dict] | None,
        enable_thinking: bool,
        reasoning_effort: str | None,
    ) -> dict[str, Any]:
        """构造 LiteLLM 调用参数。"""
        completion_kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if api_key is not None:
            completion_kwargs["api_key"] = api_key
        if tools is not None:
            completion_kwargs["tools"] = tools

        if not self._is_deepseek_model(model):
            return completion_kwargs

        # DeepSeek 通过 extra_body 控制 thinking
        extra_body: dict[str, Any] = {
            "thinking": {"type": "enabled" if enable_thinking else "disabled"},
        }
        if enable_thinking and reasoning_effort is not None:
            extra_body["reasoning_effort"] = reasoning_effort
        completion_kwargs["extra_body"] = extra_body
        return completion_kwargs

    @staticmethod
    def _is_deepseek_model(model: str) -> bool:
        return model.startswith("deepseek/")

    @staticmethod
    def _extract_completion_text(response: Any) -> str | None:
        """从非流式补全响应提取最终文本。"""
        try:
            choices = getattr(response, "choices", None)
            if choices:
                first_choice = choices[0]
                message = getattr(first_choice, "message", None)
                if message is not None:
                    content = getattr(message, "content", None)
                    if content is not None:
                        return content
                    if isinstance(message, dict):
                        dict_content = message.get("content")
                        if dict_content is not None:
                            return str(dict_content)
                if isinstance(first_choice, dict):
                    dict_message = first_choice.get("message")
                    if isinstance(dict_message, dict) and dict_message.get("content") is not None:
                        return str(dict_message["content"])
        except Exception as e:
            logger.debug("_extract_completion_text failed: %s", e)
        return None

    def _normalize_single_chunk(self, chunk: Any) -> LLMChunk | None:
        """将单个 LiteLLM chunk 归一化为 LLMChunk。"""
        content = self._extract_content(chunk)
        thinking = self._extract_thinking(chunk)
        tool_calls = self._extract_tool_calls(chunk)
        finish_reason = self._extract_finish_reason(chunk)
        usage = self._extract_usage(chunk)

        if content is not None or thinking is not None or tool_calls is not None or finish_reason is not None or usage is not None:
            return LLMChunk(
                content=content,
                thinking=thinking,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                usage=usage,
            )
        return None

    def _extract_content(self, chunk: Any) -> str | None:
        """从 LiteLLM chunk 提取 content。"""
        try:
            if hasattr(chunk, "choices"):
                choices = chunk.choices
                if choices and len(choices) > 0:
                    choice = choices[0]
                    if hasattr(choice, "delta"):
                        delta = choice.delta
                        if hasattr(delta, "content"):
                            return delta.content
            return None
        except Exception as e:
            logger.debug("_extract_content failed: %s", e)
            return None

    def _extract_thinking(self, chunk: Any) -> str | None:
        """从 chunk 提取思考内容（兼容 DeepSeek/OpenAI 推理模型）。"""
        try:
            if hasattr(chunk, "choices"):
                choices = chunk.choices
                if choices and len(choices) > 0:
                    choice = choices[0]
                    if hasattr(choice, "delta"):
                        delta = choice.delta

                        # 优先级: reasoning_content > reasoning > thinking
                        if hasattr(delta, "reasoning_content"):
                            reasoning = delta.reasoning_content
                            if reasoning is not None:
                                return reasoning

                        if hasattr(delta, "reasoning"):
                            reasoning = delta.reasoning
                            if reasoning is not None:
                                return reasoning

                        if hasattr(delta, "thinking"):
                            thinking = delta.thinking
                            if thinking is not None:
                                return thinking

            return None
        except Exception as e:
            logger.debug("_extract_thinking failed: %s", e)
            return None

    def _extract_tool_calls(self, chunk: Any) -> list[dict] | None:
        """从 chunk 提取 tool_calls 增量片段。"""
        try:
            if not hasattr(chunk, "choices"):
                return None
            choices = chunk.choices
            if not choices or len(choices) == 0:
                return None
            choice = choices[0]
            if not hasattr(choice, "delta"):
                return None
            delta = choice.delta
            if not hasattr(delta, "tool_calls"):
                return None
            tool_calls = delta.tool_calls
            if not tool_calls:
                return None

            result: list[dict] = []
            for tc in tool_calls:
                index = getattr(tc, "index", 0)
                tc_id = getattr(tc, "id", None)
                function_name = None
                arguments = None

                if hasattr(tc, "function"):
                    func = tc.function
                    function_name = getattr(func, "name", None)
                    arguments = getattr(func, "arguments", None)

                tool_call_delta: dict = {"index": index}
                if tc_id is not None:
                    tool_call_delta["id"] = tc_id
                if function_name is not None:
                    tool_call_delta["function_name"] = function_name
                if arguments is not None:
                    tool_call_delta["arguments"] = arguments

                result.append(tool_call_delta)

            return result if result else None
        except Exception as e:
            logger.debug("_extract_tool_calls failed: %s", e)
            return None

    def _extract_finish_reason(self, chunk: Any) -> str | None:
        """从 chunk 提取 finish_reason。"""
        try:
            if hasattr(chunk, "choices"):
                choices = chunk.choices
                if choices and len(choices) > 0:
                    choice = choices[0]
                    if hasattr(choice, "finish_reason"):
                        finish_reason = choice.finish_reason
                        if finish_reason is not None:
                            return finish_reason
            return None
        except Exception as e:
            logger.debug("_extract_finish_reason failed: %s", e)
            return None

    def _extract_usage(self, chunk: Any) -> UsageInfo | None:
        """从 chunk 提取 Token 用量信息。"""
        try:
            if not hasattr(chunk, "usage"):
                return None
            usage = chunk.usage
            if usage is None:
                return None

            prompt_tokens = getattr(usage, "prompt_tokens", 0)
            completion_tokens = getattr(usage, "completion_tokens", 0)
            total_tokens = getattr(usage, "total_tokens", 0)

            if prompt_tokens == 0 and completion_tokens == 0 and total_tokens == 0:
                return None

            return UsageInfo(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            )
        except Exception as e:
            logger.debug("_extract_usage failed: %s", e)
            return None
