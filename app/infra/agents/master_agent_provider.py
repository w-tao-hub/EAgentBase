"""主智能体提供者实现（v2 多 profile 版本）。

该模块提供：
- load_master_agent() 工具函数：从 Settings 与提示词文件加载主 Agent 元信息。
- MasterAgentProvider 类：作为 profile 注册中心，管理默认 profile 和子代理 profile。
"""

from __future__ import annotations  # 启用未来注解，避免前向引用问题

from pathlib import Path  # 导入路径处理类

from app.config import Settings  # 导入应用配置对象
from app.core.models.agent import Agent, AgentExecutionProfile  # 导入 Agent 领域模型和执行配置
from app.core.models.error import ErrorCode  # 导入错误码枚举
from app.infra.logging import get_logger  # 导入日志获取函数

# 获取模块级日志器
logger = get_logger(__name__)

# 当前文件所在目录，用于定位 master_prompt.md
_CURRENT_DIR = Path(__file__).parent


def load_master_agent(settings: Settings) -> Agent:
    """从 Settings 与 master_prompt.md 加载主 Agent 元信息。

    这是一次性工具函数，用于在容器装配阶段从配置和提示词文件
    构造主 Agent 的静态元信息。

    Args:
        settings: 应用配置对象，提供主智能体静态字段。

    Returns:
        Agent: 配置中定义的主智能体实例。

    Raises:
        FileNotFoundError: 如果提示词文件不存在。
    """
    # 从仓库内提示词文件读取系统提示词，保持文本配置仍由文件维护。
    prompt_path = _CURRENT_DIR / "master_prompt.md"
    logger.debug("读取主智能体提示词: %s", prompt_path)
    system_prompt = prompt_path.read_text(encoding="utf-8").strip()

    # 使用 Settings 中的静态字段构造 Agent。
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
        """初始化 profile 注册中心。

        Args:
            default_profile: 默认主 Agent 的执行 profile，必须提供。
            child_profiles: 子代理类型名称到执行 profile 的映射，默认为空字典。
        """
        # 保存默认 profile，供 get_default_profile() 和 get_default() 使用。
        self._default_profile = default_profile
        # 保存子代理 profile 映射，未提供时初始化为空字典。
        self._child_profiles = dict(child_profiles or {})
        # 将所有已注册的 profile 按 agent_id 索引，方便按 ID 查找。
        self._all_profiles: dict[str, AgentExecutionProfile] = {
            default_profile.agent_id: default_profile,
        }
        # 将子代理 profile 也加入按 ID 索引的集合中。
        for profile in self._child_profiles.values():
            self._all_profiles[profile.agent_id] = profile

        logger.debug(
            "MasterAgentProvider 初始化完成: default=%s, children=%s",
            default_profile.agent_id,
            list(self._child_profiles.keys()),
        )

    def get_default(self) -> Agent:
        """返回默认 Agent 的静态配置。

        Returns:
            Agent: 默认 Agent 的静态配置实例。
        """
        return self._default_profile.agent

    def get_default_profile(self) -> AgentExecutionProfile:
        """获取默认主 Agent 的执行 profile。

        Returns:
            AgentExecutionProfile: 默认 agent 的完整执行配置。
        """
        return self._default_profile

    def get_profile(self, agent_id: str) -> AgentExecutionProfile:
        """按 agent_id 获取对应的执行 profile。

        Args:
            agent_id: Agent 唯一标识。

        Returns:
            AgentExecutionProfile: 对应 agent 的完整执行配置。

        Raises:
            ValueError: 指定的 agent_id 未注册。
        """
        try:
            return self._all_profiles[agent_id]
        except KeyError:
            raise ValueError(f"Agent profile 未找到: {agent_id}")

    def get_child_profile(self, subagent_type: str) -> AgentExecutionProfile:
        """按子代理类型名称获取对应的执行 profile。

        Args:
            subagent_type: 子代理类型名称（如 "Plan"、"Explore" 等）。

        Returns:
            AgentExecutionProfile: 对应子代理的完整执行配置。

        Raises:
            ValueError: 指定的子代理类型未注册，错误码为 UNKNOWN_SUBAGENT。
        """
        profile = self._child_profiles.get(subagent_type)
        if profile is None:
            raise ValueError(f"{ErrorCode.UNKNOWN_SUBAGENT.value}: {subagent_type}")
        return profile

    def get_sub_agents(self) -> list[Agent]:
        """获取所有已注册的子智能体列表。

        Returns:
            list[Agent]: 子智能体 Agent 元信息列表。
        """
        return [profile.agent for profile in self._child_profiles.values()]
