"""查询当前 session 下可恢复子代理列表的工具。"""

from __future__ import annotations

import json
from typing import Any

from app.core.models.execution_context import ExecutionContext
from app.core.models.tool import Tool, ToolResult
from app.infra.store.redis_session_store import RedisSessionStore


class ListResumableSubagentsTool(Tool):
    """读取当前 session 下全部可恢复子代理的最新摘要。"""

    def __init__(self, session_store: RedisSessionStore) -> None:
        """初始化查询工具。"""
        self._session_store = session_store

    @property
    def name(self) -> str:
        """返回工具标识符。"""
        return "ListResumableSubagents"

    @property
    def description(self) -> str:
        """返回工具描述。"""
        return "列出当前会话中全部可恢复子代理，返回 resume_id、subagent_type 和最新 description。"

    @property
    def input_schema(self) -> dict[str, Any]:
        """返回 JSON Schema 输入参数定义。"""
        return {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }

    def is_read_only(self) -> bool:
        """声明该工具为只读工具。"""
        return True

    async def call(self, input: dict[str, Any], context: ExecutionContext) -> ToolResult:
        """执行查询，返回当前 session 下全部可恢复子代理摘要列表。

        过滤掉 subagent_type="" 的占位条目（此类条目由 ensure_session_child_registered
        在 child 消息落库但未经过正式摘要写入时产生，不应视为可恢复子代理）。
        """
        summaries = (await self._session_store.list_session_child_summaries(context.session_id)) or []
        items = [
            {
                "resume_id": summary.resume_id,
                "subagent_type": summary.subagent_type,
                "description": summary.description,
            }
            for summary in summaries
            if summary.subagent_type  # 过滤掉子代理类型为空的占位条目
        ]
        return ToolResult(content=json.dumps({"items": items}, ensure_ascii=False))
