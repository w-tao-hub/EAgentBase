"""Agent 基础设施模块。

提供 AgentProvider 的具体实现，包括从内置配置文件加载的 MasterAgentProvider。
"""

from __future__ import annotations  # 启用未来注解，避免前向引用问题

from app.infra.agents.master_agent_provider import MasterAgentProvider  # 导出主智能体提供者

__all__ = ["MasterAgentProvider"]  # 模块公开接口列表
