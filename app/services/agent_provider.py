"""AgentProvider 协议定义。

该模块定义了 Agent 提供者的抽象协议，允许不同的提供者实现
（如静态配置、数据库查询、动态加载等）接入系统。

v2 扩展为支持多 profile 的注册中心，同时保持对旧 get_default 方法的兼容。
"""

from __future__ import annotations

from typing import Protocol

from app.core.models.agent import Agent, AgentExecutionProfile


class AgentProvider(Protocol):
    """Agent 提供者协议（v2 多 profile 版本）。

    实现该协议的类必须同时提供 Agent 元信息访问和执行配置访问。
    这是依赖倒置原则（DIP）的应用：高层模块依赖此抽象协议，而非具体实现。
    """

    def get_default(self) -> Agent:
        """获取系统默认的 Agent 静态配置。"""
        ...

    def get_default_profile(self) -> AgentExecutionProfile:
        """获取系统默认的 Agent 执行 profile。"""
        ...

    def get_profile(self, agent_id: str) -> AgentExecutionProfile:
        """按 agent_id 获取对应的执行 profile。"""
        ...

    def get_child_profile(self, subagent_type: str) -> AgentExecutionProfile:
        """按子代理类型名称获取对应的执行 profile。"""
        ...

    def get_sub_agents(self) -> list[Agent]:
        """获取所有已注册的子智能体列表。"""
        ...

    def get_master_profile_by_name(self, name: str) -> AgentExecutionProfile:
        """按主代理名称获取执行 profile。

        Args:
            name: 主代理名称（与 MASTER_AGENT_DEFINITIONS 中的 name 对应）

        Returns:
            对应的 AgentExecutionProfile

        Raises:
            ValueError: 当指定名称的主代理不存在时
        """
        ...

    def get_master_profile(self, agent_id: str) -> AgentExecutionProfile:
        """按主代理 ID 获取执行 profile。

        Args:
            agent_id: 主代理唯一标识

        Returns:
            对应的 AgentExecutionProfile

        Raises:
            ValueError: 当指定 ID 的主代理不存在时
        """
        ...
