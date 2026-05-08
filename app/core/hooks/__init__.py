"""Hook 模块导出面。

`app.core.hooks` 对外继续保持稳定导入路径，
内部实现拆分到更细的子模块中，避免把完整实现堆进 `__init__.py`。
"""

from app.core.hooks.base import ModelHook, ToolHook  # 导出 Hook 基类
from app.core.hooks.errors import HookExecutionError  # 导出 Hook 执行异常
from app.core.hooks.guard import NoOpStreamTextGuard, StreamTextGuard  # 导出流式文本守卫抽象
from app.core.hooks.persist_large_tool_result_hook import (  # 导出大工具结果持久化 Hook 与相关常量。
    MAX_TOOL_RESULT_CHARACTERS,
    PersistLargeToolResultHook,
    QUERY_TOOL_RESULT_NAME,
    TOOL_RESULT_PREVIEW_CHARACTERS,
)
from app.core.hooks.pipeline import ModelHookPipeline, ToolHookPipeline  # 导出 Hook 管线
from app.core.hooks.types import ModelRequest, ModelResponse, ToolRequest, ToolResponse  # 导出 Hook 载体


__all__ = [
    "HookExecutionError",
    "MAX_TOOL_RESULT_CHARACTERS",
    "ModelHook",
    "ModelHookPipeline",
    "ModelRequest",
    "ModelResponse",
    "NoOpStreamTextGuard",
    "PersistLargeToolResultHook",
    "QUERY_TOOL_RESULT_NAME",
    "StreamTextGuard",
    "TOOL_RESULT_PREVIEW_CHARACTERS",
    "ToolHook",
    "ToolHookPipeline",
    "ToolRequest",
    "ToolResponse",
]
