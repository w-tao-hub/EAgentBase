"""大工具结果持久化 Hook。

负责在 after_tool 阶段识别超大工具输出，
将完整内容写入 Redis，并把最终返回结果改写为预览占位文本。
"""

from __future__ import annotations  # 启用未来注解，避免运行时前向引用问题。

from typing import Protocol  # 导入协议类型，为 store 定义最小依赖面。

from app.core.hooks.base import ToolHook  # 导入工具 Hook 基类，接入现有 Hook 管线。
from app.core.hooks.types import ToolResponse  # 导入工具响应载体，便于改写结果。
from app.core.models.tool import ToolResult  # 导入工具结果模型，构造改写后的结果。


MAX_TOOL_RESULT_CHARACTERS = 15000  # 超过该字符数时，视为需要持久化的大工具结果。
TOOL_RESULT_PREVIEW_CHARACTERS = 2000  # 占位文本内仅展示前 2000 字符预览。
QUERY_TOOL_RESULT_NAME = "query_tool_result"  # 查询工具结果工具本身不参与二次替换。


class ToolResultPersistenceStore(Protocol):
    """大工具结果存储最小协议。"""

    async def persist_result(self, session_id: str, tool_name: str, content: str) -> str:
        """持久化完整工具结果并返回可查询的 key。"""
        ...


class PersistLargeToolResultHook(ToolHook):
    """在 after_tool 阶段持久化超大工具结果。"""

    def __init__(self, store: ToolResultPersistenceStore) -> None:
        """初始化 Hook。"""
        super().__init__()  # 复用默认 fail-closed 行为，确保占位结果与 Redis 保存保持一致。
        self._store = store  # 保存 store，供 after_tool 阶段写入完整结果。

    async def after_tool(self, response: ToolResponse, context) -> ToolResponse:
        """在工具执行完成后按需持久化超大结果。"""
        if response.tool_name == QUERY_TOOL_RESULT_NAME:  # 查询工具结果工具返回完整正文，不再走二次替换。
            return response

        if len(response.result.content) <= MAX_TOOL_RESULT_CHARACTERS:  # 未超过阈值时保持原样透传。
            return response

        persisted_key = await self._store.persist_result(  # 先保存完整输出，再生成占位结果。
            session_id=context.session_id,
            tool_name=response.tool_name,
            content=response.result.content,
        )
        preview = response.result.content[:TOOL_RESULT_PREVIEW_CHARACTERS]  # 仅截取前缀作为可读预览。
        placeholder_content = (
            "<persisted-output>\n"
            f"输出过大. 完整输出已保存至：（{persisted_key}）\n\n"
            "**你可通过 'QueryToolResult' 工具进行查询完整结果**\n"
            "预览 :\n"
            f"{preview}\n"
            "...\n"
            "</persisted-output>"
        )
        rewritten_result = ToolResult(  # 仅替换 content，本轮错误位和附带消息保持不变。
            content=placeholder_content,
            is_error=response.result.is_error,
            stored_message=response.result.stored_message,
        )
        return ToolResponse(  # 返回改写后的新响应对象，避免原地修改共享对象。
            tool_name=response.tool_name,
            tool_call_id=response.tool_call_id,
            result=rewritten_result,
        )
