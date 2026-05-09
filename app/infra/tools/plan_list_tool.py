"""计划列表工具实现。

提供 PlanListTool，负责列出当前会话中的计划任务摘要。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict

from app.core.models.tool import Tool, ToolResult
from app.services.task_service import TaskService

if TYPE_CHECKING:  # 仅在类型检查时导入，避免循环依赖
    from app.core.models.execution_context import ExecutionContext


class PlanListTool(Tool):
    """列出计划任务工具。"""

    def __init__(self, task_service: TaskService) -> None:
        """初始化列出计划任务工具。

        Args:
            task_service: 任务业务服务实例。
        """
        self._task_service = task_service  # 保存任务服务引用

    @property
    def name(self) -> str:
        """工具标识符。"""
        return "plan_list"

    @property
    def description(self) -> str:
        """工具描述。"""
        return (
            "列出当前会话中所有未删除任务的摘要信息。"
            "用于检查整体进度、查找可处理的任务或确认依赖关系。"
        )

    @property
    def input_schema(self) -> Dict[str, Any]:
        """JSON Schema 输入参数定义。"""
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }

    async def call(self, input: Dict[str, Any], context: "ExecutionContext") -> ToolResult:
        """执行列出任务。

        Args:
            input: 工具输入参数（空字典）。
            context: 执行上下文，包含 session_id。

        Returns:
            ToolResult: 成功返回任务摘要数组 JSON。
        """
        try:
            result = await self._task_service.list_tasks(session_id=context.resolve_plan_session_id())
            return ToolResult(content=result, is_error=False)
        except Exception as e:
            return ToolResult(
                content=f"列出任务失败: {str(e)}",
                is_error=True,
            )
