"""LLM Chunk 模型定义。

提供流式响应中 chunk 的统一格式，支持文本内容和工具调用增量片段。
"""

from __future__ import annotations  # 启用未来注解

from dataclasses import dataclass  # 导入数据类装饰器
from typing import TYPE_CHECKING, Any  # 导入类型检查标记和任意类型

if TYPE_CHECKING:  # 仅在类型检查时导入，避免运行时循环依赖
    from app.core.runtime.agent_runtime import UsageInfo  # 导入 UsageInfo 类型


@dataclass  # 定义为数据类
class LLMChunk:
    """归一化的 LLM 响应 chunk。

    用于统一不同 LLM 提供商的响应格式，支持文本内容和工具调用增量片段。
    替代原有的 Chunk 类，提供更完整的流式响应支持。
    """

    content: str | None = None  # chunk 的文本内容，可能为None（纯工具调用时）
    thinking: str | None = None  # 思考内容（推理模型的思考过程），可能为None
    tool_calls: list[dict] | None = None  # 工具调用增量片段列表，每个字典包含 index、id、function_name、arguments
    finish_reason: str | None = None  # 完成原因，如 "stop"、"tool_calls" 等
    usage: UsageInfo | None = None  # Token 用量信息

    def to_dict(self) -> dict[str, Any]:  # 转换为字典
        """将 LLMChunk 转换为字典格式。

        Returns:
            包含所有非空字段的字典
        """
        result: dict[str, Any] = {}  # 初始化空结果字典
        if self.content is not None:  # 如果内容不为空
            result["content"] = self.content  # 添加内容字段
        if self.thinking is not None:  # 如果思考内容不为空
            result["thinking"] = self.thinking  # 添加思考内容字段
        if self.tool_calls is not None:  # 如果工具调用列表不为空
            # tool_calls 已经是字典列表，直接使用
            result["tool_calls"] = self.tool_calls  # 直接使用字典列表
        if self.finish_reason is not None:  # 如果完成原因不为空
            result["finish_reason"] = self.finish_reason  # 添加完成原因字段
        if self.usage is not None:  # 如果用量信息不为空
            # 将 UsageInfo dataclass 转换为字典
            result["usage"] = {  # 添加用量信息字段（字典格式）
                "prompt_tokens": self.usage.prompt_tokens,
                "completion_tokens": self.usage.completion_tokens,
                "total_tokens": self.usage.total_tokens,
            }
        return result  # 返回结果字典
