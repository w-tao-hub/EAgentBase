"""Runtime 核心模块。

提供 Agent 运行时所需的上下文构建、流式执行、历史视图重建、摘要规划与持久化等核心能力。
"""

from __future__ import annotations  # # 启用未来注解，避免前向引用问题

from app.core.runtime.context_builder import ContextBuilder  # # 导出上下文构建器
from app.core.runtime.context_history_view import (  # # 导出历史视图模块
    ContextHistoryView,  # # 统一历史视图数据类
    ContextHistoryViewBuilder,  # # 历史视图构建器
)
from app.core.runtime.context_summary_planner import (  # # 导出摘要规划模块
    ContextSummaryPlanner,  # # 摘要规划器
    SummaryCompressionResult,  # # 摘要压缩结果
)
from app.core.runtime.context_summary_persistence import (  # # 导出摘要持久化模块
    SummaryPersistenceCoordinator,  # # 摘要持久化协作者
    SummaryPersistencePlan,  # # 摘要持久化计划
)
from app.core.runtime.agent_runtime import (  # # 导出 AgentRuntime 和数据类
    AgentRuntime,  # # Agent 运行时类
    TurnComplete,  # # 单次调用完成标记
    ToolCall,  # # 工具调用数据类
    Function,  # # 函数调用数据类
    UsageInfo,  # # Token 用量信息
)

__all__ = [  # # 模块公开接口列表
    "AgentRuntime",  # # Agent 运行时
    "ContextBuilder",  # # 上下文构建器
    "ContextHistoryView",  # # 历史视图数据类
    "ContextHistoryViewBuilder",  # # 历史视图构建器
    "ContextSummaryPlanner",  # # 摘要规划器
    "Function",  # # 函数调用
    "SummaryCompressionResult",  # # 摘要压缩结果
    "SummaryPersistenceCoordinator",  # # 摘要持久化协作者
    "SummaryPersistencePlan",  # # 摘要持久化计划
    "ToolCall",  # # 工具调用
    "TurnComplete",  # # 单次调用完成标记
    "UsageInfo",  # # Token 用量信息
]
