"""计划创建工具实现。

提供 PlanCreateTool，负责为当前会话创建结构化计划任务。
"""

from __future__ import annotations  # 启用未来注解

from typing import TYPE_CHECKING, Any, Dict  # 导入类型提示

from app.core.models.tool import Tool, ToolResult  # 导入工具基类与结果模型
from app.services.task_service import TaskService  # 导入任务业务服务

if TYPE_CHECKING:  # 仅在类型检查时导入，避免循环依赖
    from app.core.models.execution_context import ExecutionContext


class PlanCreateTool(Tool):
    """创建计划任务工具。"""

    def __init__(self, task_service: TaskService) -> None:
        """初始化创建计划任务工具。

        Args:
            task_service: 任务业务服务实例。
        """
        self._task_service = task_service  # 保存任务服务引用

    @property
    def name(self) -> str:
        """工具标识符。"""
        return "plan_create"

    @property
    def description(self) -> str:
        """工具描述。"""
        return (
            "为当前编码会话创建一个结构化的任务，帮助跟踪进度、组织复杂任务。"
            "复杂/多步骤任务、规划模式、多事项请求优先使用此工具建立任务列表。"
        )

    @property
    def input_schema(self) -> Dict[str, Any]:
        """JSON Schema 输入参数定义。"""
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "subject": {
                    "description": "任务的简短标题",
                    "type": "string",
                },
                "description": {
                    "description": "需要完成内容的详细描述",
                    "type": "string",
                },
                "activeForm": {
                    "description": "任务进行中时显示的现在进行时描述，例如：正在运行测试",
                    "type": "string",
                },
                "metadata": {
                    "description": "附加到任务的任意元数据",
                    "type": "object",
                    "propertyNames": {"type": "string"},
                    "additionalProperties": True,
                },
            },
            "required": ["subject", "description"],
            "additionalProperties": False,
        }

    async def call(self, input: Dict[str, Any], context: "ExecutionContext") -> ToolResult:
        """执行创建任务。

        Args:
            input: 工具输入参数。
            context: 执行上下文，包含 session_id。

        Returns:
            ToolResult: 创建成功返回完整任务 JSON；失败返回错误信息。
        """
        subject = input.get("subject", "")
        description = input.get("description", "")
        active_form = input.get("activeForm")
        metadata = input.get("metadata")

        if not subject or not description:
            return ToolResult(
                content="subject 和 description 为必填字段",
                is_error=True,
            )

        try:
            result = await self._task_service.create_task(
                session_id=context.resolve_plan_session_id(),
                subject=subject,
                description=description,
                active_form=active_form,
                metadata=metadata,
            )
            return ToolResult(content=result, is_error=False)
        except Exception as e:
            return ToolResult(
                content=f"创建任务失败: {str(e)}",
                is_error=True,
            )
