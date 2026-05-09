"""主智能体提供者实现（v2 多 profile 版本）。

该模块提供：
- load_master_agent() 工具函数：从 Settings 与提示词文件加载主 Agent 元信息。
- MasterAgentProvider 类：作为 profile 注册中心，管理默认 profile 和子代理 profile。
"""

from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.core.models.agent import Agent, AgentExecutionProfile
from app.core.models.error import ErrorCode
from app.infra.logging import get_logger

logger = get_logger(__name__)

_CURRENT_DIR = Path(__file__).parent


def load_master_agent(settings: Settings) -> Agent:
    """从 Settings 与 master_prompt.md 加载主 Agent 元信息。"""
    prompt_path = _CURRENT_DIR / "master_prompt.md"
    logger.debug("读取主智能体提示词: %s", prompt_path)
    system_prompt = prompt_path.read_text(encoding="utf-8").strip()

    agent = Agent(
        agent_id=settings.master_agent_id,
        name=settings.master_agent_name,
        model=settings.master_agent_model,
        system_prompt=system_prompt,
        temperature=settings.master_agent_temperature,
        reasoning_effort=settings.master_agent_reasoning_effort,
    )

    logger.debug(
        "主智能体加载完成: agent_id=%s, model=%s",
        agent.agent_id,
        agent.model,
    )
    return agent


class MasterAgentProvider:
    """多 profile 注册中心。

    该类实现了 AgentProvider 协议，管理默认 profile 和子代理 profile 的注册与查找。
    与 v1 版本不同，v2 不再在内部加载 Agent——Agent 元信息由调用方在构造时通过
    AgentExecutionProfile 注入。
    """

    def __init__(
        self,
        *,
        default_profile: AgentExecutionProfile,
        child_profiles: dict[str, AgentExecutionProfile] | None = None,
    ) -> None:
        self._default_profile = default_profile
        self._child_profiles = dict(child_profiles or {})
        self._all_profiles: dict[str, AgentExecutionProfile] = {
            default_profile.agent_id: default_profile,
        }
        for profile in self._child_profiles.values():
            self._all_profiles[profile.agent_id] = profile

        logger.debug(
            "MasterAgentProvider 初始化完成: default=%s, children=%s",
            default_profile.agent_id,
            list(self._child_profiles.keys()),
        )

    def get_default(self) -> Agent:
        return self._default_profile.agent

    def get_default_profile(self) -> AgentExecutionProfile:
        return self._default_profile

    def get_profile(self, agent_id: str) -> AgentExecutionProfile:
        try:
            return self._all_profiles[agent_id]
        except KeyError:
            raise ValueError(f"Agent profile 未找到: {agent_id}")

    def get_child_profile(self, subagent_type: str) -> AgentExecutionProfile:
        """按子代理类型名称获取执行 profile。"""
        profile = self._child_profiles.get(subagent_type)
        if profile is None:
            raise ValueError(f"{ErrorCode.UNKNOWN_SUBAGENT.value}: {subagent_type}")
        return profile

    def get_sub_agents(self) -> list[Agent]:
        return [profile.agent for profile in self._child_profiles.values()]
