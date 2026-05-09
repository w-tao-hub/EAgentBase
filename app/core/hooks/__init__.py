"""Hook 模块导出面。

`app.core.hooks` 对外继续保持稳定导入路径，
内部实现拆分到更细的子模块中，避免把完整实现堆进 `__init__.py`。
"""

from app.core.hooks.base import ModelHook, ToolHook
from app.core.hooks.errors import HookExecutionError
from app.core.hooks.guard import NoOpStreamTextGuard, StreamTextGuard
from app.core.hooks.persist_large_tool_result_hook import (
    MAX_TOOL_RESULT_CHARACTERS,
    PersistLargeToolResultHook,
    QUERY_TOOL_RESULT_NAME,
    TOOL_RESULT_PREVIEW_CHARACTERS,
)
from app.core.hooks.pipeline import ModelHookPipeline, ToolHookPipeline
from app.core.hooks.types import ModelRequest, ModelResponse, ToolRequest, ToolResponse


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
