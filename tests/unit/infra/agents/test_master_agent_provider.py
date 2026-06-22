"""MasterAgentProvider 的单元测试（v2 多 profile 版本）。

测试覆盖：
- 内置主代理定义的稳定性
- load_master_agent() 按代码定义和配置加载主 Agent
- MasterAgentProvider 作为多 profile 注册中心的行为（按名称/ID查找、校验）
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.core.hooks import ToolHookPipeline
from app.core.models.agent import Agent, AgentExecutionProfile, AgentPromptSource
from app.core.models.error import ErrorCode
from app.core.models.tool import ToolRegistry
from app.infra.agents.master_agent_provider import (
    DEFAULT_MASTER_AGENT_NAME,  # 默认主代理名称常量
    MASTER_AGENT_DEFINITIONS,  # 内置主代理定义元组
    MasterAgentDefinition,  # 主代理静态定义数据类
    MasterAgentProvider,  # 多 profile 注册中心
    load_master_agent,  # 主 Agent 加载工具函数
)


# ============================================================================
# 辅助函数
# ============================================================================


def _build_settings(
    *,
    master_agent_model: str = "deepseek/custom-model",
    master_agent_temperature: float = 0.35,
    master_agent_reasoning_effort: str = "high",
) -> Settings:
    """构造测试专用 Settings，显式覆盖主智能体运行时配置字段。

    注意：主代理的 agent_id 和 name 已从环境配置迁移到代码内置定义，
    不再作为 Settings 字段，因此本函数不再提供这些参数。
    """
    return Settings(
        redis_url="redis://localhost:6379/0",
        master_agent_model=master_agent_model,
        master_agent_temperature=master_agent_temperature,
        master_agent_reasoning_effort=master_agent_reasoning_effort,
    )


def _build_profile(agent_id: str) -> AgentExecutionProfile:
    """构建最小测试用 AgentExecutionProfile。

    直接构造 Agent 和 AgentExecutionProfile，不依赖测试假对象，
    使测试数据更加自包含和可读。

    Args:
        agent_id: profile 和 agent 的标识

    Returns:
        最小可用 AgentExecutionProfile 实例
    """
    agent = Agent(
        agent_id=agent_id,
        name=agent_id,
        model="test-model",
        system_prompt=f"System prompt for {agent_id}",
        temperature=0.2,
    )
    return AgentExecutionProfile(
        agent_id=agent.agent_id,
        agent=agent,
        prompt_source=AgentPromptSource(kind="file", path=f"{agent_id}.md"),
        runtime=object(),  # 使用最小 object 占位，测试不关心运行时
        tool_registry=ToolRegistry(),
        tool_hook_pipeline=ToolHookPipeline(),
        max_turns=10,
    )


# ============================================================================
# 内置主代理定义与加载测试
# ============================================================================


def test_builtin_master_agent_definitions_are_stable() -> None:
    """验证内置主代理清单稳定，且 agent_id 与 name 保持一致。"""
    definitions = {definition.name: definition for definition in MASTER_AGENT_DEFINITIONS}

    assert DEFAULT_MASTER_AGENT_NAME == "default"
    assert set(definitions) == {"default", "plan"}
    assert definitions["default"].agent_id == "default"
    assert definitions["default"].prompt_file == "master_prompt.md"
    assert definitions["plan"].agent_id == "plan"
    assert definitions["plan"].prompt_file == "plan_master_prompt.md"


def test_load_master_agent_uses_definition_identity_and_settings_model() -> None:
    """验证主代理身份来自代码定义，模型参数继续来自 Settings。

    主代理的 agent_id 和 name 由 MasterAgentDefinition 控制，
    而 model、temperature、reasoning_effort 继续从 Settings 读取。
    """
    settings = _build_settings(
        master_agent_reasoning_effort="max",
    )
    definition = MasterAgentDefinition(
        agent_id="custom",
        name="custom",
        prompt_file="master_prompt.md",
    )

    agent = load_master_agent(settings=settings, definition=definition)

    assert agent.agent_id == "custom"
    assert agent.name == "custom"
    assert agent.model == "deepseek/custom-model"
    assert agent.temperature == 0.35
    assert agent.reasoning_effort == "max"
    assert agent.system_prompt.startswith("你是一个乐于助人且专业的 AI 编程助手。")


# ============================================================================
# MasterAgentProvider 多 profile 注册中心测试
# ============================================================================


def test_master_agent_provider_returns_master_profiles_by_name_and_id() -> None:
    """验证 provider 同时支持按主代理名称和 ID 查找。

    新构造函数使用 master_profiles 字典收纳多个主代理 profile，
    同时继续支持 child_profiles 字典管理子代理。
    """
    default_profile = _build_profile("default")
    plan_profile = _build_profile("plan")
    worker_profile = _build_profile("Worker")
    provider = MasterAgentProvider(
        master_profiles={
            "default": default_profile,
            "plan": plan_profile,
        },
        child_profiles={"Worker": worker_profile},
    )

    assert provider.get_default() is default_profile.agent
    assert provider.get_default_profile() is default_profile
    assert provider.get_master_profile_by_name("default") is default_profile
    assert provider.get_master_profile_by_name("plan") is plan_profile
    assert provider.get_master_profile("plan") is plan_profile
    assert provider.get_profile("Worker") is worker_profile
    assert provider.get_sub_agents() == [worker_profile.agent]


def test_master_agent_provider_rejects_unknown_master_name() -> None:
    """验证未知主代理名称返回明确业务错误。

    当按名称查找不存在的主代理时，应抛出带 UNKNOWN_MASTER_AGENT 错误码的 ValueError。
    """
    provider = MasterAgentProvider(master_profiles={"default": _build_profile("default")})

    with pytest.raises(ValueError, match=f"{ErrorCode.UNKNOWN_MASTER_AGENT.value}: ghost"):
        provider.get_master_profile_by_name("ghost")


def test_master_agent_provider_requires_default_master() -> None:
    """验证 provider 启动时必须包含 default 主代理。

    构造时不传 default 主代理应抛出带 INVALID_MASTER_AGENT_CONFIG 错误码的 ValueError。
    """
    with pytest.raises(ValueError, match=f"{ErrorCode.INVALID_MASTER_AGENT_CONFIG.value}: 缺少默认主代理 default"):
        MasterAgentProvider(master_profiles={"plan": _build_profile("plan")})


def test_master_agent_provider_get_default_profile_identity() -> None:
    """测试 get_default_profile() 返回的 profile 与 get_default().agent 一致。"""
    master_profile = _build_profile("default")
    provider = MasterAgentProvider(master_profiles={"default": master_profile})

    # 多次调用 get_default_profile() 应返回同一对象
    assert provider.get_default_profile() is master_profile
    assert provider.get_default_profile() is provider.get_default_profile()
    # get_default() 返回的 Agent 与 profile.agent 一致
    assert provider.get_default() is master_profile.agent


def test_master_agent_provider_get_profile_unknown_raises_valueerror() -> None:
    """测试按不存在的 agent_id 查找 profile 时抛出 ValueError。"""
    provider = MasterAgentProvider(master_profiles={"default": _build_profile("default")})

    # 查找不存在的 agent_id 应抛出 ValueError
    with pytest.raises(ValueError, match="Agent profile 未找到"):
        provider.get_profile("nonexistent")


def test_master_agent_provider_get_child_profile_unknown_raises_unknown_subagent() -> None:
    """测试按不存在的子代理类型查找 profile 时抛出带 UNKNOWN_SUBAGENT 错误码的 ValueError。"""
    provider = MasterAgentProvider(master_profiles={"default": _build_profile("default")})

    # 查找不存在的子代理类型应抛出带 UNKNOWN_SUBAGENT 错误码的 ValueError
    with pytest.raises(ValueError, match=f"{ErrorCode.UNKNOWN_SUBAGENT.value}: UnknownAgent"):
        provider.get_child_profile("UnknownAgent")


def test_master_agent_provider_get_sub_agents_with_multiple_children() -> None:
    """测试 get_sub_agents() 能正确返回多个子代理的 Agent 列表。"""
    plan_profile = _build_profile("Plan")
    explore_profile = _build_profile("Explore")
    # 构造包含两个子代理的注册中心
    provider = MasterAgentProvider(
        master_profiles={"default": _build_profile("default")},
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
    # 只传入默认主代理，不传入子代理
    provider = MasterAgentProvider(master_profiles={"default": _build_profile("default")})

    # get_sub_agents() 应返回空列表
    assert provider.get_sub_agents() == []
