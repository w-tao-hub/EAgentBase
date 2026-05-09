"""Runtime 核心模块。

提供 Agent 运行时所需的上下文构建、流式执行、历史视图重建、摘要规划与持久化等核心能力。
"""

from __future__ import annotations

from app.core.runtime.context_builder import ContextBuilder
from app.core.runtime.context_history_view import (
    ContextHistoryView,
    ContextHistoryViewBuilder,
)
from app.core.runtime.context_summary_planner import (
    ContextSummaryPlanner,
    SummaryCompressionResult,
)
from app.core.runtime.context_summary_persistence import (
    SummaryPersistenceCoordinator,
    SummaryPersistencePlan,
)
from app.core.runtime.agent_runtime import (
    AgentRuntime,
    TurnComplete,
    ToolCall,
    Function,
    UsageInfo,
)

__all__ = [
    "AgentRuntime",
    "ContextBuilder",
    "ContextHistoryView",
    "ContextHistoryViewBuilder",
    "ContextSummaryPlanner",
    "Function",
    "SummaryCompressionResult",
    "SummaryPersistenceCoordinator",
    "SummaryPersistencePlan",
    "ToolCall",
    "TurnComplete",
    "UsageInfo",
]
