"""LLM Chunk 模型定义。

提供流式响应中 chunk 的统一格式，支持文本内容和工具调用增量片段。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.runtime.agent_runtime import UsageInfo


@dataclass
class LLMChunk:
    """归一化的 LLM 响应 chunk。

    用于统一不同 LLM 提供商的响应格式，支持文本内容和工具调用增量片段。
    替代原有的 Chunk 类，提供更完整的流式响应支持。
    """

    content: str | None = None
    thinking: str | None = None
    tool_calls: list[dict] | None = None
    finish_reason: str | None = None
    usage: UsageInfo | None = None

    def to_dict(self) -> dict[str, Any]:
        """将 LLMChunk 转换为字典格式。

        Returns:
            包含所有非空字段的字典
        """
        result: dict[str, Any] = {}
        if self.content is not None:
            result["content"] = self.content
        if self.thinking is not None:
            result["thinking"] = self.thinking
        if self.tool_calls is not None:
            result["tool_calls"] = self.tool_calls
        if self.finish_reason is not None:
            result["finish_reason"] = self.finish_reason
        if self.usage is not None:
            result["usage"] = {
                "prompt_tokens": self.usage.prompt_tokens,
                "completion_tokens": self.usage.completion_tokens,
                "total_tokens": self.usage.total_tokens,
            }
        return result
