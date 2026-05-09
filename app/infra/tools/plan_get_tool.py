"""计划详情查询工具实现。

提供 PlanGetTool，负责读取当前会话中的单个计划任务详情。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict

from app.core.models.tool import Tool, ToolResult
from app.services.task_service import TaskService

if TYPE_CHECKING:  # 仅在类型检查时导入，避免循环依赖
    from app.core.models.execution_context import ExecutionContext


class PlanGetTool(Tool):
    """获取计划任务详情工具。"""

    def __init__(self, task_service: TaskService) -> None:
        """初始化获取计划任务工具。

        Args:
            task_service: 任务业务服务实例。
        """
        self._task_service = task_service  # 保存任务服务引用

    @property
    def name(self) -> str:
        """工具标识符。"""
        return "plan_get"

    @property
    def description(self) -> str:
        """工具描述。"""
        return (
            "通过任务 ID 获取当前会话中指定任务的完整详情，包括描述、状态、依赖关系等。"
            "开始处理前优先使用此工具确认上下文。"
        )

    @property
    def input_schema(self) -> Dict[str, Any]:
        """JSON Schema 输入参数定义。"""
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "taskId": {
                    "description": "要获取的任务 ID",
                    "type": "string",
                },
            },
            "required": ["taskId"],
            "additionalProperties": False,
        }

    async def call(self, input: Dict[str, Any], context: "ExecutionContext") -> ToolResult:
        """执行获取任务。

        Args:
            input: 工具输入参数。
            context: 执行上下文，包含 session_id。

        Returns:
            ToolResult: 成功返回完整任务 JSON；不存在返回错误。
        """
        task_id = input.get("taskId", "")
        if not task_id:
            return ToolResult(
                content="taskId 为必填字段",
                is_error=True,
            )

        result = await self._task_service.get_task(
            session_id=context.resolve_plan_session_id(),
            task_id=task_id,
        )
        if result is None:
            return ToolResult(
                content=f"任务不存在: {task_id}",
                is_error=True,
            )
        return ToolResult(content=result, is_error=False)
