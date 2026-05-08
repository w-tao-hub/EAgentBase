"""MasterAgentProvider 的单元测试（v2 多 profile 版本）。

测试覆盖：
- MasterAgentProvider 作为多 profile 注册中心的行为
- load_master_agent() 工具函数从 Settings 和文件加载主 Agent 元信息
"""

from __future__ import annotations  # 启用未来注解，避免前向引用问题

import pytest  # 导入 pytest 测试框架

from app.config import Settings  # 导入应用配置
from app.core.hooks import ToolHookPipeline  # 导入工具 Hook 管线
from app.core.models.agent import AgentExecutionProfile, AgentPromptSource  # 导入执行配置和 prompt 来源
from app.core.models.error import ErrorCode  # 导入错误码枚举
from app.core.models.tool import ToolRegistry  # 导入工具注册表
from app.infra.agents.master_agent_provider import (  # 导入被试模块
    MasterAgentProvider,  # 多 profile 注册中心
    _CURRENT_DIR,  # 主智能体提示词文件所在目录
    load_master_agent,  # 主 Agent 加载工具函数
)
from tests.fakes import FakeAgentRuntime, create_fake_agent  # 导入测试替身


# ============================================================================
# 辅助函数
# ============================================================================


def _build_settings(
    *,
    master_agent_id: str = "custom-master-agent",
    master_agent_name: str = "Custom Master Agent",
    master_agent_model: str = "deepseek/custom-model",
    master_agent_temperature: float = 0.35,
    master_agent_reasoning_effort: str = "high",
) -> Settings:
    """构造测试专用 Settings，显式覆盖主智能体配置字段。"""
    return Settings(
        redis_url="redis://localhost:6379/0",
        master_agent_id=master_agent_id,
        master_agent_name=master_agent_name,
        master_agent_model=master_agent_model,
        master_agent_temperature=master_agent_temperature,
        master_agent_reasoning_effort=master_agent_reasoning_effort,
    )


def _build_profile(agent_id: str) -> AgentExecutionProfile:
    """构造测试用 AgentExecutionProfile。

    使用 FakeAgentRuntime 和空的 ToolRegistry / ToolHookPipeline 构造最小可用 profile，
    供 MasterAgentProvider 各方法的单元测试使用。

    Args:
        agent_id: profile 和 agent 的标识

    Returns:
        最小可用 AgentExecutionProfile 实例
    """
    # 基于 create_fake_agent 创建 Agent，覆盖 agent_id 和 name 使其与传入参数一致
    agent = create_fake_agent().model_copy(update={"agent_id": agent_id, "name": agent_id})
    # 构造最小可用 profile
    return AgentExecutionProfile(
        agent_id=agent_id,  # profile 标识
        agent=agent,  # 测试 Agent 元信息
        prompt_source=AgentPromptSource(kind="file", path=f"{agent_id}.md"),  # 模拟文件来源
        runtime=FakeAgentRuntime(),  # 使用假运行时
        tool_registry=ToolRegistry(),  # 空工具注册表
        tool_hook_pipeline=ToolHookPipeline(),  # 空 Hook 管线
        max_turns=10,  # 测试用最大轮数
    )


# ============================================================================
# load_master_agent() 工具函数测试
# ============================================================================


def test_load_master_agent_returns_agent_from_settings():
    """测试 load_master_agent() 能从 Settings 正确加载主智能体。"""
    # 显式构造带有自定义字段的 Settings，确保函数读取的是配置对象而不是硬编码默认值。
    settings = _build_settings()
    # 调用工具函数加载主 Agent，验证运行时配置注入链路。
    agent = load_master_agent(settings)

    # 断言主智能体标识、名称、模型全部来自 Settings。
    assert agent.agent_id == "custom-master-agent"
    assert agent.name == "Custom Master Agent"
    assert agent.model == "deepseek/custom-model"
    # 验证 system_prompt 仍然来自仓库内的 master_prompt.md，而不是被迁移到环境变量。
    assert agent.system_prompt.startswith("你是一个乐于助人且专业的 AI 编程助手。")


def test_load_master_agent_preserves_float_temperature():
    """测试主智能体温度字段会按浮点数透传到 Agent。"""
    # 使用非默认浮点值，保护温度配置在 Settings -> Agent 链路中的精度传递。
    agent = load_master_agent(_build_settings(master_agent_temperature=0.73))
    assert agent.temperature == 0.73


def test_load_master_agent_preserves_reasoning_effort() -> None:
    """测试主智能体思考强度字段会按配置透传到 Agent。"""
    agent = load_master_agent(_build_settings(master_agent_reasoning_effort="max"))
    assert agent.reasoning_effort == "max"


# ============================================================================
# MasterAgentProvider 多 profile 注册中心测试
# ============================================================================


def test_master_agent_provider_returns_profiles_by_id() -> None:
    """测试 MasterAgentProvider 能按 agent_id 或子代理类型名称查找 profile。"""
    # 构造 master 和 Plan 两个测试 profile
    master_profile = _build_profile("master-agent")
    plan_profile = _build_profile("Plan")
    # 使用新版 profile 注册中心构造函数
    provider = MasterAgentProvider(
        default_profile=master_profile,  # 注入默认主 profile
        child_profiles={"Plan": plan_profile},  # 注入 Plan 子代理 profile
    )

    # get_default() 应返回默认 profile 的 Agent 静态配置
    assert provider.get_default() is master_profile.agent
    # get_default_profile() 应返回默认 profile 本身
    assert provider.get_default_profile() is master_profile
    # get_profile() 应按 agent_id 定位到对应 profile
    assert provider.get_profile("master-agent") is master_profile
    # get_child_profile() 应按子代理类型名称定位到对应 profile
    assert provider.get_child_profile("Plan") is plan_profile
    # get_sub_agents() 应返回所有子代理的 Agent 列表
    assert provider.get_sub_agents() == [plan_profile.agent]


def test_master_agent_provider_get_default_profile_identity() -> None:
    """测试 get_default_profile() 返回的 profile 与 get_default().agent 一致。"""
    master_profile = _build_profile("master-agent")
    provider = MasterAgentProvider(default_profile=master_profile)

    # 多次调用 get_default_profile() 应返回同一对象
    assert provider.get_default_profile() is master_profile
    assert provider.get_default_profile() is provider.get_default_profile()
    # get_default() 返回的 Agent 与 profile.agent 一致
    assert provider.get_default() is master_profile.agent


def test_master_agent_provider_get_profile_unknown_raises_valueerror() -> None:
    """测试按不存在的 agent_id 查找 profile 时抛出 ValueError。"""
    provider = MasterAgentProvider(default_profile=_build_profile("master-agent"))

    # 查找不存在的 agent_id 应抛出 ValueError
    with pytest.raises(ValueError, match="Agent profile 未找到"):
        provider.get_profile("nonexistent")


def test_master_agent_provider_get_child_profile_unknown_raises_unknown_subagent() -> None:
    """测试按不存在的子代理类型查找 profile 时抛出带 UNKNOWN_SUBAGENT 错误码的 ValueError。"""
    provider = MasterAgentProvider(default_profile=_build_profile("master-agent"))

    # 查找不存在的子代理类型应抛出带 UNKNOWN_SUBAGENT 错误码的 ValueError
    with pytest.raises(ValueError, match=f"{ErrorCode.UNKNOWN_SUBAGENT.value}: UnknownAgent"):
        provider.get_child_profile("UnknownAgent")


def test_master_agent_provider_get_sub_agents_with_multiple_children() -> None:
    """测试 get_sub_agents() 能正确返回多个子代理的 Agent 列表。"""
    plan_profile = _build_profile("Plan")
    explore_profile = _build_profile("Explore")
    # 构造包含两个子代理的注册中心
    provider = MasterAgentProvider(
        default_profile=_build_profile("master-agent"),
        child_profiles={"Plan": plan_profile, "Explore": explore_profile},
    )

    sub_agents = provider.get_sub_agents()
    # 应返回 2 个子代理的 Agent
    assert len(sub_agents) == 2
    # 子代理列表中的 Agent 应与原始 profile.agent 一致
    assert plan_profile.agent in sub_agents
    assert explore_profile.agent in sub_agents


def test_master_agent_provider_get_sub_agents_empty_by_default() -> None:
    """测试未传入子代理时 get_sub_agents() 返回空列表。"""
    # 只传入默认 profile，不传入子代理
    provider = MasterAgentProvider(default_profile=_build_profile("master-agent"))

    # get_sub_agents() 应返回空列表
    assert provider.get_sub_agents() == []


def test_master_agent_provider_prompt_file_exists():
    """测试主智能体提示词文件必须存在。"""
    # 本次迁移只移动静态字段到 Settings，不迁移提示词文件，因此仍需保证 prompt 文件存在。
    prompt_path = _CURRENT_DIR / "master_prompt.md"
    assert prompt_path.exists(), f"主智能体提示词文件不存在: {prompt_path}"
