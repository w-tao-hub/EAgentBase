"""SkillTool 单元测试。"""

from __future__ import annotations  # 启用未来注解，避免前向引用问题。

from pathlib import Path  # 导入 Path，便于构造临时 skill 目录。

import pytest  # 导入 pytest，编写异步单元测试。

from app.core.models.agent import Agent  # 导入 Agent，构造执行上下文。
from app.core.models.execution_context import ExecutionContext  # 导入执行上下文。
from app.infra.skills.catalog import SkillCatalog  # 导入技能索引，供 SkillTool 使用。
from app.infra.tools.skill_tool import SkillTool  # 导入被测 SkillTool。


def _context() -> ExecutionContext:
    """构造 SkillTool 测试所需的最小执行上下文。"""
    return ExecutionContext(
        run_id="run-1",
        session_id="session-1",
        metadata={},
        agent=Agent(
            agent_id="agent-1",
            name="Skill Test Agent",
            model="gpt-4.1-mini",
            system_prompt="test",
            temperature=0.0,
        ),
    )


def _write_skill(skill_root: Path, name: str, description: str, body: str) -> None:
    """写入一个测试 skill 目录与 SKILL.md 文件。"""
    skill_dir = skill_root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n",
        encoding="utf-8",
    )


def test_skill_catalog_scans_skill_docs_from_root_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """测试 SkillCatalog 会从启动目录下的 skills 目录扫描技能。"""
    skills_root = tmp_path / "skills"
    _write_skill(skills_root, "demo", "演示技能", "这是 demo skill。")
    monkeypatch.chdir(tmp_path)

    catalog = SkillCatalog.discover()

    skill_doc = catalog.get("demo")
    assert skill_doc.name == "demo"
    assert skill_doc.description == "演示技能"
    assert skill_doc.content == "这是 demo skill。\n"


def test_skill_catalog_uses_directory_name_when_front_matter_name_is_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """测试 front matter 缺少 name 时会回退为目录名。"""
    skill_dir = tmp_path / "skills" / "demo"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname:\ndescription: 演示技能\n---\n正文\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    catalog = SkillCatalog.discover()

    assert catalog.get("demo").name == "demo"


def test_skill_catalog_fails_when_skill_names_conflict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """测试两个目录声明同名 skill 时会直接失败。"""
    skills_root = tmp_path / "skills"
    _write_skill(skills_root, "alpha", "技能 A", "A")
    _write_skill(skills_root, "beta", "技能 B", "B")
    (skills_root / "beta" / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: 技能 B\n---\nB\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="alpha"):
        SkillCatalog.discover()


@pytest.mark.asyncio
async def test_skill_tool_returns_tool_result_and_meta_stored_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """测试 SkillTool 成功时会返回提示文本与 isMeta 存储消息。"""
    skills_root = tmp_path / "skills"
    _write_skill(skills_root, "demo", "演示技能", "这是完整技能文档。")
    monkeypatch.chdir(tmp_path)
    catalog = SkillCatalog.discover()
    tool = SkillTool(catalog)

    result = await tool.call({"skill": "demo"}, _context())

    assert result.is_error is False
    assert "demo" in result.content
    assert result.stored_message is not None
    assert result.stored_message.role == "user"
    assert result.stored_message.is_meta is True
    assert result.stored_message.content == (
        "<skill_name>demo</skill_name>"
        "<skill_message>这是完整技能文档。\n</skill_message>"
    )


@pytest.mark.asyncio
async def test_skill_tool_returns_error_when_skill_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """测试请求不存在的 skill 时返回错误结果且不附带消息。"""
    (tmp_path / "skills").mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)
    tool = SkillTool(SkillCatalog.discover())

    result = await tool.call({"skill": "missing"}, _context())

    assert result.is_error is True
    assert result.stored_message is None
    assert "missing" in result.content
