"""子代理配置加载与 profile 组装测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.core.hooks import ToolHookPipeline
from app.core.models.tool import ToolResult
from app.infra.agents.custom_sub_agent_loader import CustomSubAgentLoader
from app.infra.agents.default_sub_agents.definitions import DEFAULT_SUB_AGENT_DEFINITIONS
from app.infra.agents.hook_profiles import HookProfileRegistry
from app.infra.agents.profile_builder import SubAgentProfileBuilder
from app.infra.skills.catalog import SkillCatalog, SkillDocument
from tests.fakes import FakeAgentRuntime, FakeTool


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
        hook_profile=None,
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
    hook_profiles = HookProfileRegistry({"review-hooks": ToolHookPipeline()})

    profile = SubAgentProfileBuilder(
        settings=Settings(redis_url="redis://localhost:6379/0"),
        runtime=FakeAgentRuntime(),
        tool_catalog=_tool_catalog(),
        hook_profiles=hook_profiles,
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


def test_hook_profile_registry_rejects_unknown_name() -> None:
    """测试 HookProfileRegistry 对非法 hook_profile 名称抛出 INVALID_SUBAGENT_CONFIG。

    当子代理定义中引用了未在代码侧预注册的 Hook profile 名称时，
    HookProfileRegistry 应拒绝隐式回退到默认 Hook，直接抛出配置错误。
    """
    registry = HookProfileRegistry({"review-hooks": ToolHookPipeline()})  # 创建注册表，只注册 review-hooks

    with pytest.raises(ValueError, match="INVALID_SUBAGENT_CONFIG"):  # 断言抛出包含正确错误码的异常
        registry.get("non-existent-profile")  # 尝试获取不存在的 profile


def test_profile_builder_rejects_invalid_hook_profile(tmp_path: Path) -> None:
    """测试 profile builder 中 hook_profile 不存在时报 INVALID_SUBAGENT_CONFIG。

    当默认子代理定义中 hook_profile 指向一个未在 HookProfileRegistry
    中注册的名称时，SubAgentProfileBuilder.build_default_profile 应抛异常。
    """
    from app.config import Settings  # 导入应用配置

    prompt_file = tmp_path / "plan.md"  # 创建临时 prompt 文件
    prompt_file.write_text("你是计划代理。", encoding="utf-8")  # 写入测试 prompt 内容
    definition = DEFAULT_SUB_AGENT_DEFINITIONS[0].with_overrides(  # 基于默认 Plan 定义覆盖配置
        prompt_file="plan.md",  # 使用相对 prompt 文件名，确保命中 hook_profile 校验分支
        hook_profile="non-existent",  # 指向不存在的 Hook profile
    )
    hook_profiles = HookProfileRegistry({"review-hooks": ToolHookPipeline()})  # 创建注册表，只注册 review-hooks

    with pytest.raises(ValueError, match="INVALID_SUBAGENT_CONFIG"):  # 断言抛出包含正确错误码的异常
        SubAgentProfileBuilder(  # 创建 profile 组装器
            settings=Settings(redis_url="redis://localhost:6379/0"),  # 最小配置
            runtime=FakeAgentRuntime(),  # 假运行时
            tool_catalog={},  # 空工具目录
            hook_profiles=hook_profiles,  # 注入不包含 "non-existent" 的注册表
            skill_catalog=SkillCatalog({}),  # 空 skill 目录
            default_prompt_root=tmp_path,  # 使用临时目录作为 prompt 根目录
        ).build_default_profile(definition)  # 组装 profile，应触发 ValueError
