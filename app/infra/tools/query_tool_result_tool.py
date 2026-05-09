"""QueryToolResult 工具实现。

提供对当前会话下已持久化大工具结果的按 key 查询能力。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict

from app.core.models.tool import Tool, ToolResult

if TYPE_CHECKING:  # 仅在类型检查时导入，避免运行时不必要耦合。
    from app.core.models.execution_context import ExecutionContext
    from app.infra.store.redis_tool_result_store import RedisToolResultStore


class QueryToolResultTool(Tool):
    """查询当前会话下已持久化大工具结果的只读工具。"""

    def __init__(self, tool_result_store: "RedisToolResultStore") -> None:
        """初始化查询工具。"""
        self._tool_result_store = tool_result_store  # 保存 store 引用，供查询完整结果时使用。

    @property
    def name(self) -> str:
        """返回工具标识符。"""
        return "query_tool_result"

    @property
    def description(self) -> str:
        """返回工具描述。"""
        return "根据持久化结果 key 查询当前会话内已保存的大工具完整输出。仅允许查询当前会话产生的结果。"

    @property
    def input_schema(self) -> Dict[str, Any]:
        """返回 JSON Schema 输入参数定义。"""
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "key": {
                    "description": "需要查询的持久化工具结果 Redis key",
                    "type": "string",
                },
            },
            "required": ["key"],
            "additionalProperties": False,
        }

    def is_read_only(self) -> bool:
        """声明该工具为只读工具。"""
        return True

    async def call(self, input: Dict[str, Any], context: "ExecutionContext") -> ToolResult:
        """执行查询。"""
        key = str(input.get("key", "")).strip()  # 读取并归一化查询 key，避免 None 等异常值渗透。
        if not key:  # key 为空时直接返回受控错误结果。
            return ToolResult(content="key 为必填字段", is_error=True)

        if not self._tool_result_store.is_tool_result_key(key):  # 先过滤明显非法的命名空间输入。
            return ToolResult(content=f"工具结果 key 格式非法: {key}", is_error=True)

        persisted_result = await self._tool_result_store.get_result(  # 仅允许查询当前会话下的结果。
            key=key,
            session_id=context.session_id,
        )
        if persisted_result is None:  # key 不存在或不属于当前 session 时统一收敛为错误。
            return ToolResult(content=f"工具结果不存在，或不属于当前会话: {key}", is_error=True)

        return ToolResult(content=persisted_result.content, is_error=False)  # 成功返回完整正文。
