"""计划更新工具实现。

提供 PlanUpdateTool，负责更新当前会话中的计划任务字段与状态。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict

from app.core.models.tool import Tool, ToolResult
from app.services.task_service import TaskService

if TYPE_CHECKING:  # 仅在类型检查时导入，避免循环依赖
    from app.core.models.execution_context import ExecutionContext


class PlanUpdateTool(Tool):
    """更新计划任务工具。"""

    def __init__(self, task_service: TaskService) -> None:
        """初始化更新计划任务工具。

        Args:
            task_service: 任务业务服务实例。
        """
        self._task_service = task_service  # 保存任务服务引用

    @property
    def name(self) -> str:
        """工具标识符。"""
        return "plan_update"

    @property
    def description(self) -> str:
        """工具描述。"""
        return (
            "更新当前会话中的任务字段、状态或依赖关系。"
            "开始执行时标记状态为 in_progress；完成后标记为 completed。"
            "状态设为 deleted 可永久删除任务。"
        )

    @property
    def input_schema(self) -> Dict[str, Any]:
        """JSON Schema 输入参数定义。"""
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "taskId": {
                    "description": "要更新的任务 ID",
                    "type": "string",
                },
                "subject": {
                    "description": "任务的新标题",
                    "type": "string",
                },
                "description": {
                    "description": "任务的新描述",
                    "type": "string",
                },
                "activeForm": {
                    "description": "任务进行中时显示的现在进行时描述",
                    "type": "string",
                },
                "status": {
                    "description": "任务的新状态",
                    "anyOf": [
                        {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                        },
                        {
                            "type": "string",
                            "const": "deleted",
                        },
                    ],
                },
                "addBlocks": {
                    "description": "被当前任务阻塞的任务 ID 列表",
                    "type": "array",
                    "items": {"type": "string"},
                },
                "addBlockedBy": {
                    "description": "阻塞当前任务的任务 ID 列表",
                    "type": "array",
                    "items": {"type": "string"},
                },
                "owner": {
                    "description": "任务的新负责人",
                    "type": "string",
                },
                "metadata": {
                    "description": "要合并到任务中的元数据。将键设为 null 可删除该键。",
                    "type": "object",
                    "propertyNames": {"type": "string"},
                    "additionalProperties": True,
                },
            },
            "required": ["taskId"],
            "additionalProperties": False,
        }

    async def call(self, input: Dict[str, Any], context: "ExecutionContext") -> ToolResult:
        """执行更新任务。

        Args:
            input: 工具输入参数。
            context: 执行上下文，包含 session_id。

        Returns:
            ToolResult: 成功返回更新后任务 JSON 或删除确认 JSON；失败返回错误信息。
        """
        task_id = input.get("taskId", "")
        if not task_id:
            return ToolResult(
                content="taskId 为必填字段",
                is_error=True,
            )

        try:
            result = await self._task_service.update_task(
                session_id=context.resolve_plan_session_id(),
                task_id=task_id,
                subject=input.get("subject"),
                description=input.get("description"),
                active_form=input.get("activeForm"),
                status=input.get("status"),
                owner=input.get("owner"),
                metadata=input.get("metadata"),
                add_blocks=input.get("addBlocks"),
                add_blocked_by=input.get("addBlockedBy"),
            )
        except ValueError as e:
            return ToolResult(
                content=str(e),
                is_error=True,
            )
        except Exception as e:
            return ToolResult(
                content=f"更新任务失败: {str(e)}",
                is_error=True,
            )

        if result is None:
            return ToolResult(
                content=f"任务不存在: {task_id}",
                is_error=True,
            )
        return ToolResult(content=result, is_error=False)
