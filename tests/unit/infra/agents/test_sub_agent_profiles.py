"""子代理配置加载与 profile 组装测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.core.hooks import ToolHook, ToolHookPipeline
from app.core.models.tool import ToolResult
from app.infra.agents.custom_sub_agent_loader import CustomSubAgentLoader
from app.infra.agents.default_sub_agents.definitions import DEFAULT_SUB_AGENT_DEFINITIONS
from app.infra.agents.hook_profiles import HookRegistry
from app.infra.agents.profile_builder import SubAgentProfileBuilder
from app.infra.skills.catalog import SkillCatalog, SkillDocument
from tests.fakes import FakeAgentRuntime, FakeTool


class _FakeToolHook(ToolHook):
    """测试用最小 ToolHook 实现。"""
    async def before_tool(self, request, context):
        return request
    async def after_tool(self, response, context):
        return response


def _tool_catalog() -> dict[str, FakeTool]:
    """构造测试用工具目录。"""
    return {
        "Read": FakeTool("Read", "读文件", {"type": "object"}, ToolResult("read")),
        "Task": FakeTool("Task", "派发子代理", {"type": "object"}, ToolResult("task")),
    }


def test_default_plan_definition_uses_relative_prompt_file() -> None:
    """测试默认 Plan 只配置相对 prompt 文件名。"""
    plan = DEFAULT_SUB_AGENT_DEFINITIONS[0]

    assert plan.name == "Worker"
    assert plan.prompt_file == "worker.md"
    assert not plan.prompt_file.startswith("app/")


def test_custom_loader_rejects_model_and_unknown_fields(tmp_path: Path) -> None:
    """测试自定义子代理禁用 model 和未知字段。"""
    agent_file = tmp_path / "bad.md"
    agent_file.write_text(
        "---\n"
        "name: code-view\n"
        "description: 检查代码\n"
        "model: sonnet\n"
        "---\n"
        "正文\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="禁用字段"):
        CustomSubAgentLoader(tmp_path).load()


def test_custom_loader_skips_missing_directory(tmp_path: Path) -> None:
    """测试 agents 目录不存在时按空目录处理。"""
    missing_dir = tmp_path / "agents"

    definitions = CustomSubAgentLoader(missing_dir).load()

    assert definitions == []


def test_profile_builder_filters_task_and_skips_missing_skill(tmp_path: Path) -> None:
    """测试 profile 组装会过滤 Task，且缺失 skill 不报错。"""
    prompt_file = tmp_path / "plan.md"
    prompt_file.write_text("你是计划代理。", encoding="utf-8")
    definition = DEFAULT_SUB_AGENT_DEFINITIONS[0].with_overrides(
        prompt_file="plan.md",
        tools=("Read", "Task"),
        skills=("known-skill", "missing-skill"),
        tool_hook_profiles=None,
    )
    skill_catalog = SkillCatalog(
        {
            "known-skill": SkillDocument(
                name="known-skill",
                description="已安装技能",
                path=Path("skills/known-skill/SKILL.md"),
                content="技能正文",
            )
        }
    )
    hook_registry = HookRegistry(tool_hooks={"review-hooks": _FakeToolHook()}, model_hooks={})

    profile = SubAgentProfileBuilder(
        settings=Settings(redis_url="redis://localhost:6379/0"),
        runtime=FakeAgentRuntime(),
        tool_catalog=_tool_catalog(),
        hook_registry=hook_registry,
        skill_catalog=skill_catalog,
        default_prompt_root=tmp_path,
    ).build_default_profile(definition)

    assert profile.agent_id == "Worker"
    assert "Read" in profile.tool_registry
    assert "Task" not in profile.tool_registry
    assert profile.skills == ("known-skill",)
    assert any("技能正文" in message for message in profile.extra_system_messages)


def test_custom_loader_rejects_name_conflict_with_default_plan(tmp_path: Path) -> None:
    """测试自定义子代理不能与默认 Plan 重名。"""
    agent_file = tmp_path / "Plan.md"
    agent_file.write_text(
        "---\n"
        "name: Plan\n"
        "description: 冲突测试\n"
        "---\n"
        "正文\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="默认子代理重名"):
        CustomSubAgentLoader(tmp_path, reserved_names={"Plan"}).load()


def test_hook_registry_rejects_unknown_name() -> None:
    """测试 HookRegistry 对非法 tool_hook 名称抛出 INVALID_SUBAGENT_CONFIG。"""
    registry = HookRegistry(tool_hooks={"review-hooks": _FakeToolHook()}, model_hooks={})

    with pytest.raises(ValueError, match="INVALID_SUBAGENT_CONFIG"):
        registry.get_tool_hook("non-existent")


def test_profile_builder_rejects_invalid_tool_hook_profiles(tmp_path: Path) -> None:
    """测试 profile builder 中 tool_hook 名称不存在时报 INVALID_SUBAGENT_CONFIG。"""
    from app.config import Settings

    prompt_file = tmp_path / "plan.md"
    prompt_file.write_text("你是计划代理。", encoding="utf-8")
    definition = DEFAULT_SUB_AGENT_DEFINITIONS[0].with_overrides(
        prompt_file="plan.md",
        tool_hook_profiles=("non-existent",),
    )
    hook_registry = HookRegistry(tool_hooks={"review-hooks": _FakeToolHook()}, model_hooks={})

    with pytest.raises(ValueError, match="INVALID_SUBAGENT_CONFIG"):
        SubAgentProfileBuilder(
            settings=Settings(redis_url="redis://localhost:6379/0"),
            runtime=FakeAgentRuntime(),
            tool_catalog={},
            hook_registry=hook_registry,
            skill_catalog=SkillCatalog({}),
            default_prompt_root=tmp_path,
        ).build_default_profile(definition)
