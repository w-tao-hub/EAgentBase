"""Hook 串行执行管线定义。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Sequence

from app.core.hooks.base import ModelHook, ToolHook
from app.core.hooks.errors import HookExecutionError
from app.core.hooks.types import ModelRequest, ModelResponse, ToolRequest, ToolResponse

if TYPE_CHECKING:
    from app.core.models.execution_context import ExecutionContext


logger = logging.getLogger(__name__)


class ModelHookPipeline:
    """模型 Hook 串行执行管线。"""

    def __init__(self, hooks: Sequence[ModelHook] | None = None) -> None:
        """初始化模型 Hook 列表。"""
        self._hooks = list(hooks or [])

    @property
    def hooks(self) -> list[ModelHook]:
        """返回当前装配的模型 Hook 列表副本。"""
        return list(self._hooks)

    async def before_model(self, request: ModelRequest, context: "ExecutionContext") -> ModelRequest:
        """按顺序执行所有 before_model Hook。"""
        current_request = request
        for hook in self._hooks:
            try:
                current_request = await hook.before_model(current_request, context)
            except Exception as exc:
                if hook.fail_open:
                    logger.warning("模型 Hook before_model 失败，已按 fail-open 跳过: hook=%s, error=%s", hook.__class__.__name__, exc)
                    continue
                raise HookExecutionError("before_model", hook.__class__.__name__, exc) from exc
        return current_request

    async def after_model(self, response: ModelResponse, context: "ExecutionContext") -> ModelResponse:
        """按顺序执行所有 after_model Hook。"""
        current_response = response
        for hook in self._hooks:
            try:
                current_response = await hook.after_model(current_response, context)
            except Exception as exc:
                if hook.fail_open:
                    logger.warning("模型 Hook after_model 失败，已按 fail-open 跳过: hook=%s, error=%s", hook.__class__.__name__, exc)
                    continue
                raise HookExecutionError("after_model", hook.__class__.__name__, exc) from exc
        return current_response


class ToolHookPipeline:
    """工具 Hook 串行执行管线。"""

    def __init__(self, hooks: Sequence[ToolHook] | None = None) -> None:
        """初始化工具 Hook 列表。"""
        self._hooks = list(hooks or [])

    @property
    def hooks(self) -> list[ToolHook]:
        """返回当前装配的工具 Hook 列表副本。"""
        return list(self._hooks)

    async def before_tool(self, request: ToolRequest, context: "ExecutionContext") -> ToolRequest:
        """按顺序执行所有 before_tool Hook。"""
        current_request = request
        for hook in self._hooks:
            try:
                current_request = await hook.before_tool(current_request, context)
            except Exception as exc:
                if hook.fail_open:
                    logger.warning("工具 Hook before_tool 失败，已按 fail-open 跳过: hook=%s, error=%s", hook.__class__.__name__, exc)
                    continue
                raise HookExecutionError("before_tool", hook.__class__.__name__, exc) from exc
        return current_request

    async def after_tool(self, response: ToolResponse, context: "ExecutionContext") -> ToolResponse:
        """按顺序执行所有 after_tool Hook。"""
        current_response = response
        for hook in self._hooks:
            try:
                current_response = await hook.after_tool(current_response, context)
            except Exception as exc:
                if hook.fail_open:
                    logger.warning("工具 Hook after_tool 失败，已按 fail-open 跳过: hook=%s, error=%s", hook.__class__.__name__, exc)
                    continue
                raise HookExecutionError("after_tool", hook.__class__.__name__, exc) from exc
        return current_response
