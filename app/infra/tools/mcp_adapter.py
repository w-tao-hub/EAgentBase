"""MCP 工具适配器实现。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from app.core.models.tool import Tool, ToolResult

if TYPE_CHECKING:
    from app.core.models.execution_context import ExecutionContext


class MCPToolAdapter(Tool):
    """把远端 MCP tool 适配为项目内 Tool。"""

    def __init__(
        self,
        server_id: str,
        remote_tool_name: str,
        description: str,
        input_schema: dict[str, Any],
        session: Any,
    ) -> None:
        self._server_id = server_id
        self._remote_tool_name = remote_tool_name
        self._description = description
        self._input_schema = input_schema
        self._session = session

    @property
    def name(self) -> str:
        return f"mcp_{self._remote_tool_name}"

    @property
    def description(self) -> str:
        return self._description

    @property
    def input_schema(self) -> dict[str, Any]:
        return self._input_schema

    def is_read_only(self) -> bool:
        return False

    async def call(self, input: dict, context: "ExecutionContext") -> ToolResult:
        _ = context
        try:
            raw_result = await self._session.call_tool(self._remote_tool_name, arguments=input)
        except Exception as exc:
            return ToolResult(content=f"MCP 工具调用失败: server={self._server_id}, tool={self._remote_tool_name}, error={exc}", is_error=True)

        content_text = self._stringify_content_blocks(getattr(raw_result, "content", []))
        structured_content = getattr(raw_result, "structuredContent", None)
        if structured_content is not None:
            structured_text = json.dumps(structured_content, ensure_ascii=False, indent=2)
            if content_text:
                content_text = f"{content_text}\n\nstructuredContent:\n{structured_text}"
            else:
                content_text = f"structuredContent:\n{structured_text}"

        if not content_text:
            content_text = "MCP 工具未返回可展示内容"

        is_error = bool(getattr(raw_result, "isError", False))
        return ToolResult(content=content_text, is_error=is_error)

    def _stringify_content_blocks(self, blocks: list[Any]) -> str:
        text_parts: list[str] = []
        for block in blocks:
            text_value = getattr(block, "text", None)
            if isinstance(text_value, str) and text_value:
                text_parts.append(text_value)
                continue
            if isinstance(block, str) and block:
                text_parts.append(block)
                continue
            serialized_block = self._safe_serialize_block(block)
            if serialized_block:
                text_parts.append(serialized_block)
        return "\n".join(text_parts)

    def _safe_serialize_block(self, block: Any) -> str:
        try:
            if hasattr(block, "__dict__"):
                return json.dumps(block.__dict__, ensure_ascii=False, default=str)
            return json.dumps(block, ensure_ascii=False, default=str)
        except TypeError:
            return str(block)
