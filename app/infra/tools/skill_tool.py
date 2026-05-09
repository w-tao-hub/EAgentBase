"""SkillTool 实现。"""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.models.stored_message import StoredMessage
from app.core.models.tool import Tool, ToolResult
from app.infra.skills.catalog import SkillCatalog


class SkillTool(Tool):
    """读取 skill 文档全文并把它注入后续模型上下文。"""

    _DESCRIPTION = """在主对话中执行一个技能

当用户让你执行任务时，检查是否有匹配的可用技能。技能提供专业化能力和领域知识。

当用户提到“斜杠命令”或“/<命令名>”（例如 "/commit"、"/review-pr"）时，他们指的就是技能。使用此工具来调用它。

调用方式：
- 使用此工具，传入技能名称
- 示例：
  - `skill: "pdf"` – 调用 PDF 技能


重要说明：
- 可用技能会在对话的系统提醒消息中列出
- 当有技能匹配用户请求时，这是**强制要求**：在生成关于该任务的任何其他回复之前，先调用对应的技能工具
- 绝对不要在不实际调用此工具的情况下提及技能
- 不要调用已经在运行中的技能
- 不要将此工具用于内置 CLI 命令（如 /help、/clear 等）
- 如果在当前轮对话中看到 <skill-name> 标签，表示技能已加载——直接按指示执行，不要再调用此工具"""

    def __init__(self, catalog: SkillCatalog) -> None:
        """注入运行时 skill 索引。"""
        self._catalog = catalog  # 保存运行时索引，供调用时按名称取文档。

    @property
    def name(self) -> str:
        """返回固定工具名。"""
        return "skill"

    @property
    def description(self) -> str:
        """返回对模型展示的工具说明。"""
        return self._DESCRIPTION

    @property
    def input_schema(self) -> dict:
        """返回只包含 skill 名称的输入 schema。"""
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "skill": {
                    "description": "技能名称。例如：\"commit\"、\"review-pr\" 或 \"pdf\"",
                    "type": "string",
                }
            },
            "required": ["skill"],
            "additionalProperties": False,
        }

    async def call(self, input: dict, context) -> ToolResult:
        """读取 skill 文档并返回附带的隐藏会话消息。"""
        skill_name = str(input.get("skill", "")).strip()  # 取出请求的 skill 名称。
        if not skill_name:  # 缺失 skill 名称时直接返回错误结果，避免继续查索引。
            return ToolResult(content="skill 参数不能为空", is_error=True)

        try:
            document = self._catalog.get(skill_name)  # 从运行时索引读取 skill 文档。
        except ValueError as exc:
            return ToolResult(content=str(exc), is_error=True)

        stored_message = StoredMessage.create(
            role="user",
            content=(
                f"<skill_name>{document.name}</skill_name>"
                f"<skill_message>{document.content}</skill_message>"
            ),
            timestamp=datetime.now(timezone.utc),
            is_meta=True,
        )

        return ToolResult(
            content=f"开始执行技能：{document.name}\n技能加载完成：{document.name}",
            is_error=False,
            stored_message=stored_message,
        )
