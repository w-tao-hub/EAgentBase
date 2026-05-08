"""AgentLoop 模块，提供多轮循环编排能力。

该模块实现 AgentLoop 类，支持：
- 多轮对话循环
- 工具调用处理
- 工具错误处理
- 最大轮数限制
- 事件流输出
"""

from __future__ import annotations  # 启用未来注解

# 导出 AgentLoop 类和工具相关事件类，供外部使用
from app.core.loop.agent_loop import AgentLoop, ToolUseStartedEvent, ToolUseCompletedEvent

# 定义模块公开接口
__all__ = ["AgentLoop", "ToolUseStartedEvent", "ToolUseCompletedEvent"]  # 模块公开接口列表
