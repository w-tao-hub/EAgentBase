"""Skill 文档扫描与索引。"""

from __future__ import annotations  # 启用未来注解，避免前向引用问题。

from dataclasses import dataclass  # 导入数据类，承载扫描后的 skill 文档信息。
from pathlib import Path  # 导入 Path，用于遍历启动目录下的 skills 目录。


@dataclass(frozen=True, slots=True)
class SkillDocument:
    """表示一次扫描后得到的 skill 文档。"""

    name: str  # skill 对外暴露给模型调用的名称。
    description: str  # skill 的简短说明，用于系统提醒与工具清单。
    path: Path  # skill 文档的真实磁盘路径，便于调试与审计。
    content: str  # SKILL.md 的完整内容，供工具直接注入模型上下文。


class SkillCatalog:
    """运行时 skill 索引。

    当前版本只负责从启动目录下的 `skills/*/SKILL.md` 做一次性扫描，
    并把结果收敛成按 skill 名称检索的只读字典。
    """

    def __init__(self, documents: dict[str, SkillDocument] | None = None) -> None:
        """初始化 skill 索引。"""
        self._documents = documents or {}  # 保存扫描得到的 skill 文档映射。

    @classmethod
    def discover(cls, root: Path | None = None) -> "SkillCatalog":
        """扫描启动目录下的根级 skills 目录。"""
        project_root = root or Path.cwd()  # 默认以当前启动目录作为扫描根。
        skills_root = project_root / "skills"  # skill 固定扫描根级 skills 目录。
        documents: dict[str, SkillDocument] = {}  # 保存最终索引结果。

        if not skills_root.exists():  # skills 目录不存在时返回空索引，保持启动可用。
            return cls(documents)
        if not skills_root.is_dir():  # skills 路径存在但不是目录时直接失败，避免误配置。
            raise ValueError(f"skills 路径不是目录: {skills_root}")

        for skill_dir in sorted(path for path in skills_root.iterdir() if path.is_dir()):
            skill_file = skill_dir / "SKILL.md"  # 每个技能目录必须包含 SKILL.md。
            if not skill_file.exists():
                raise ValueError(f"skill 缺少 SKILL.md: {skill_dir}")

            raw_content = skill_file.read_text(encoding="utf-8")  # 读取 skill 原始全文，用于拆分元数据与正文。
            front_matter, body_content = cls._split_front_matter_and_body(raw_content, skill_file)  # 拆分 YAML 风格头部与真实正文。

            skill_name = str(front_matter.get("name", "")).strip() or skill_dir.name  # 空 name 回退目录名。
            description = str(front_matter.get("description", "")).strip()  # 空描述允许保留为空。

            if skill_name in documents:  # 同名 skill 会导致模型调用歧义，因此启动直接失败。
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
        """解析最小 front matter。

        当前 skill 规范只依赖 `name` 与 `description` 两个扁平键，
        因此这里使用最小解析器，避免额外引入 YAML 运行时依赖。
        返回值会把 front matter 与正文拆开，避免元数据被错误注入模型上下文。
        """
        if not raw_content.startswith("---\n"):  # 没有 front matter 时按空头部处理，保持兼容。
            return {}, raw_content

        lines = raw_content.splitlines()  # 逐行解析最小 front matter。
        end_index = None  # 记录头部结束位置。
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                end_index = index
                break

        if end_index is None:  # 头部起始存在但没有闭合时视为非法文档。
            raise ValueError(f"skill front matter 缺少结束分隔符: {skill_file}")

        data: dict[str, str] = {}
        for line in lines[1:end_index]:
            if not line.strip():  # 空行直接跳过，避免影响最小格式兼容性。
                continue
            if ":" not in line:  # 只支持 key: value 的扁平结构。
                raise ValueError(f"skill front matter 非法行: {skill_file}: {line}")
            key, value = line.split(":", 1)
            data[key.strip()] = value.strip()
        body_lines = lines[end_index + 1:]  # 正文从第二个分隔符之后开始。
        body_content = "\n".join(body_lines)  # 恢复正文内容，保留原始换行结构。
        if raw_content.endswith("\n"):  # 原文件若以换行结束，则正文也保持该语义。
            body_content += "\n"
        return data, body_content

    def get(self, name: str) -> SkillDocument:
        """按名称获取 skill 文档。"""
        try:
            return self._documents[name]
        except KeyError as exc:  # 统一转成 ValueError，便于工具层直接收敛成错误结果。
            raise ValueError(f"未知 skill: {name}") from exc

    def list_documents(self) -> list[SkillDocument]:
        """返回所有已发现的 skill 文档。"""
        return list(self._documents.values())

    def build_system_reminder(self) -> str | None:
        """构造提供给模型的 skill 提醒文案。"""
        if not self._documents:  # 没有 skill 时不注入额外系统提醒，避免无意义上下文噪音。
            return None

        skill_lines = [  # 逐条列出 skill 名称与说明，便于模型在任务匹配时发现可用技能。
            f"- {document.name}: {document.description or '无描述'}"
            for document in self.list_documents()
        ]
        return (
            "<系统提示>以下技能可通过 `skill` 工具使用：\n"
            + "\n".join(skill_lines) 
            + "\n</系统提示>"
        )
