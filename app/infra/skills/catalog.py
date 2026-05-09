"""Skill 文档扫描与索引。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SkillDocument:
    """扫描后的 skill 文档。"""

    name: str
    description: str
    path: Path
    content: str


class SkillCatalog:
    """运行时 skill 索引。

    当前版本只负责从启动目录下的 `skills/*/SKILL.md` 做一次性扫描，
    并把结果收敛成按 skill 名称检索的只读字典。
    """

    def __init__(self, documents: dict[str, SkillDocument] | None = None) -> None:
        self._documents = documents or {}

    @classmethod
    def discover(cls, root: Path | None = None) -> "SkillCatalog":
        """扫描根级 skills 目录。"""
        project_root = root or Path.cwd()
        skills_root = project_root / "skills"
        documents: dict[str, SkillDocument] = {}

        if not skills_root.exists():
            return cls(documents)
        if not skills_root.is_dir():
            raise ValueError(f"skills 路径不是目录: {skills_root}")

        for skill_dir in sorted(path for path in skills_root.iterdir() if path.is_dir()):
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                raise ValueError(f"skill 缺少 SKILL.md: {skill_dir}")

            raw_content = skill_file.read_text(encoding="utf-8")
            front_matter, body_content = cls._split_front_matter_and_body(raw_content, skill_file)

            skill_name = str(front_matter.get("name", "")).strip() or skill_dir.name
            description = str(front_matter.get("description", "")).strip()

            if skill_name in documents:
                raise ValueError(f"skill 名称冲突: {skill_name}")

            documents[skill_name] = SkillDocument(
                name=skill_name,
                description=description,
                path=skill_file,
                content=body_content,
            )

        return cls(documents)

    @staticmethod
    def _split_front_matter_and_body(raw_content: str, skill_file: Path) -> tuple[dict[str, str], str]:
        """解析最小 front matter（仅 name/description），避免引入 YAML 依赖。"""
        if not raw_content.startswith("---\n"):
            return {}, raw_content

        lines = raw_content.splitlines()
        end_index = None
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                end_index = index
                break

        if end_index is None:
            raise ValueError(f"skill front matter 缺少结束分隔符: {skill_file}")

        data: dict[str, str] = {}
        for line in lines[1:end_index]:
            if not line.strip():
                continue
            if ":" not in line:
                raise ValueError(f"skill front matter 非法行: {skill_file}: {line}")
            key, value = line.split(":", 1)
            data[key.strip()] = value.strip()
        body_lines = lines[end_index + 1:]
        body_content = "\n".join(body_lines)
        if raw_content.endswith("\n"):
            body_content += "\n"
        return data, body_content

    def get(self, name: str) -> SkillDocument:
        try:
            return self._documents[name]
        except KeyError as exc:
            raise ValueError(f"未知 skill: {name}") from exc

    def list_documents(self) -> list[SkillDocument]:
        return list(self._documents.values())

    def build_system_reminder(self) -> str | None:
        """构造供模型使用的 skill 提醒文案。"""
        if not self._documents:
            return None

        skill_lines = [
            f"- {document.name}: {document.description or '无描述'}"
            for document in self.list_documents()
        ]
        return (
            "<系统提示>以下技能可通过 `skill` 工具使用：\n"
            + "\n".join(skill_lines)
            + "\n</系统提示>"
        )
