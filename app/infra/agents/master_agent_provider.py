"""主智能体提供者实现（v2 多 profile 版本）。

该模块提供：
- MasterAgentDefinition: 代码内置的主代理静态定义数据类
- MASTER_AGENT_DEFINITIONS: 内置主代理定义清单
- load_master_agent() 工具函数：按代码定义与环境配置加载主 Agent 元信息
- MasterAgentProvider 类：作为 profile 注册中心，管理多主代理 profile 和子代理 profile
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.core.models.agent import Agent, AgentExecutionProfile
from app.core.models.error import ErrorCode
from app.infra.logging import get_logger

logger = get_logger(__name__)

_CURRENT_DIR = Path(__file__).parent

# 默认主代理名称常量
DEFAULT_MASTER_AGENT_NAME = "default"


@dataclass(frozen=True, slots=True)
class MasterAgentDefinition:
    """代码内置的主代理静态定义。

    每个主代理的标识（agent_id）、展示名称（name）和提示词文件名（prompt_file）
    均由代码内置定义控制，不再从环境变量读取。模型、温度等运行时参数仍从 Settings 读取。
    """

    agent_id: str  # 主代理唯一标识，同时作为路由键
    name: str  # 主代理展示名称
    prompt_file: str  # 提示词文件名，相对于当前模块目录


# 内置主代理定义清单，按注册顺序排列，default 始终位于首位
MASTER_AGENT_DEFINITIONS: tuple[MasterAgentDefinition, ...] = (
    MasterAgentDefinition(
        agent_id="default",
        name="default",
        prompt_file="master_prompt.md",
    ),
    MasterAgentDefinition(
        agent_id="plan",
        name="plan",
        prompt_file="plan_master_prompt.md",
    ),
)


def load_master_agent(*, settings: Settings, definition: MasterAgentDefinition) -> Agent:
    """按代码内置定义和环境模型参数加载主代理。

    主代理的标识和展示名称由 MasterAgentDefinition 控制，
    模型、温度、思考强度等运行时参数仍从 Settings 读取。

    Args:
        settings: 应用配置对象，提供模型、温度、思考强度等运行时参数
        definition: 主代理静态定义，提供 agent_id、name 和 prompt_file

    Returns:
        组装完成的 Agent 实例

    Raises:
        ValueError: 当 agent_id 与 name 不一致或 prompt 文件不存在/为空时
    """
    # 校验主代理定义完整性：agent_id 必须等于 name
    if definition.agent_id != definition.name:
        raise ValueError(
            f"{ErrorCode.INVALID_MASTER_AGENT_CONFIG.value}: "
            f"主代理 agent_id 必须等于 name: {definition.agent_id}/{definition.name}"
        )
    # 读取主代理提示词文件
    prompt_path = _CURRENT_DIR / definition.prompt_file
    if not prompt_path.exists():
        raise ValueError(
            f"{ErrorCode.INVALID_MASTER_AGENT_CONFIG.value}: 主代理 prompt 文件不存在: {prompt_path}"
        )
    system_prompt = prompt_path.read_text(encoding="utf-8").strip()
    # 校验提示词内容不为空
    if not system_prompt:
        raise ValueError(
            f"{ErrorCode.INVALID_MASTER_AGENT_CONFIG.value}: 主代理 prompt 为空: {prompt_path}"
        )

    agent = Agent(
        agent_id=definition.agent_id,
        name=definition.name,
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
    """多 profile 注册中心（v2 多主代理版本）。

    该类实现了 AgentProvider 协议，同时管理多个主代理 profile 和子代理 profile
    的注册与查找。与 v1 版本不同，v2 支持按名称和 ID 两种方式查找主代理，
    且默认主代理必须以 "default" 键传入。
    """

    def __init__(
        self,
        *,
        master_profiles: dict[str, AgentExecutionProfile],
        child_profiles: dict[str, AgentExecutionProfile] | None = None,
    ) -> None:
        # 校验必须包含 default 主代理
        if DEFAULT_MASTER_AGENT_NAME not in master_profiles:
            raise ValueError(
                f"{ErrorCode.INVALID_MASTER_AGENT_CONFIG.value}: "
                f"缺少默认主代理 {DEFAULT_MASTER_AGENT_NAME}"
            )
        # 按名称索引的主代理 profile 字典
        self._master_profiles_by_name = dict(master_profiles)
        # 按 agent_id 索引的主代理 profile 字典
        self._master_profiles_by_id: dict[str, AgentExecutionProfile] = {}
        for name, profile in self._master_profiles_by_name.items():
            # 校验主代理 profile 的名称一致性
            if profile.agent_id != name or profile.agent.name != name:
                raise ValueError(
                    f"{ErrorCode.INVALID_MASTER_AGENT_CONFIG.value}: "
                    f"主代理 profile 名称不一致: {name}/{profile.agent_id}/{profile.agent.name}"
                )
            # 校验主代理 ID 唯一性
            if profile.agent_id in self._master_profiles_by_id:
                raise ValueError(
                    f"{ErrorCode.INVALID_MASTER_AGENT_CONFIG.value}: 主代理 ID 重复: {profile.agent_id}"
                )
            self._master_profiles_by_id[profile.agent_id] = profile

        # 子代理 profile 字典
        self._child_profiles = dict(child_profiles or {})
        # 合并所有 profile（主代理 + 子代理）的 agent_id 索引
        self._all_profiles = dict(self._master_profiles_by_id)
        for profile in self._child_profiles.values():
            self._all_profiles[profile.agent_id] = profile

        logger.debug(
            "MasterAgentProvider 初始化完成: masters=%s, children=%s",
            list(self._master_profiles_by_name.keys()),
            list(self._child_profiles.keys()),
        )

    def get_default(self) -> Agent:
        """获取默认主代理的 Agent 静态配置。"""
        return self.get_default_profile().agent

    def get_default_profile(self) -> AgentExecutionProfile:
        """获取默认主代理的执行 profile。

        默认主代理始终以 DEFAULT_MASTER_AGENT_NAME 为键存储。
        """
        return self._master_profiles_by_name[DEFAULT_MASTER_AGENT_NAME]

    def get_master_profile_by_name(self, name: str) -> AgentExecutionProfile:
        """按主代理名称获取执行 profile。

        Args:
            name: 主代理名称（与 MASTER_AGENT_DEFINITIONS 中的 name 对应）

        Returns:
            对应的 AgentExecutionProfile

        Raises:
            ValueError: 当指定名称的主代理不存在时
        """
        profile = self._master_profiles_by_name.get(name)
        if profile is None:
            raise ValueError(f"{ErrorCode.UNKNOWN_MASTER_AGENT.value}: {name}")
        return profile

    def get_master_profile(self, agent_id: str) -> AgentExecutionProfile:
        """按主代理 ID 获取执行 profile。

        Args:
            agent_id: 主代理唯一标识

        Returns:
            对应的 AgentExecutionProfile

        Raises:
            ValueError: 当指定 ID 的主代理不存在时
        """
        profile = self._master_profiles_by_id.get(agent_id)
        if profile is None:
            raise ValueError(f"{ErrorCode.UNKNOWN_MASTER_AGENT.value}: {agent_id}")
        return profile

    def get_profile(self, agent_id: str) -> AgentExecutionProfile:
        """按 agent_id 获取对应的执行 profile（含主代理和子代理）。

        Args:
            agent_id: 任意已注册的 agent_id

        Returns:
            对应的 AgentExecutionProfile

        Raises:
            ValueError: 当指定 agent_id 未注册时
        """
        try:
            return self._all_profiles[agent_id]
        except KeyError:
            raise ValueError(f"Agent profile 未找到: {agent_id}")

    def get_child_profile(self, subagent_type: str) -> AgentExecutionProfile:
        """按子代理类型名称获取执行 profile。

        Args:
            subagent_type: 子代理类型名称

        Returns:
            对应的 AgentExecutionProfile

        Raises:
            ValueError: 当指定子代理类型未注册时，错误码为 UNKNOWN_SUBAGENT
        """
        profile = self._child_profiles.get(subagent_type)
        if profile is None:
            raise ValueError(f"{ErrorCode.UNKNOWN_SUBAGENT.value}: {subagent_type}")
        return profile

    def get_sub_agents(self) -> list[Agent]:
        """获取所有已注册的子智能体列表。"""
        return [profile.agent for profile in self._child_profiles.values()]
