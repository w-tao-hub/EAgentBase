"""Agent 基础设施模块。

提供 AgentProvider 的具体实现，包括从内置配置文件加载的 MasterAgentProvider。
"""

from __future__ import annotations

from app.infra.agents.master_agent_provider import MasterAgentProvider

__all__ = ["MasterAgentProvider"]
