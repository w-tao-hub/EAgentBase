"""Hook 串行执行管线定义。"""

from __future__ import annotations  # 启用未来注解，避免运行时前向引用问题

import logging  # 导入标准库日志模块，避免 core 反向依赖 infra
from typing import TYPE_CHECKING, Sequence  # 导入类型提示工具

from app.core.hooks.base import ModelHook, ToolHook  # 导入 Hook 基类
from app.core.hooks.errors import HookExecutionError  # 导入 Hook 执行异常
from app.core.hooks.types import ModelRequest, ModelResponse, ToolRequest, ToolResponse  # 导入 Hook 载体

if TYPE_CHECKING:  # 仅在类型检查阶段导入，避免循环依赖
    from app.core.models.execution_context import ExecutionContext  # 导入执行上下文类型


# 获取模块级日志器，供 fail-open 场景记录异常。
# 直接使用标准库 logging，保持 core 层不依赖 infra 包路径。
logger = logging.getLogger(__name__)


class ModelHookPipeline:
    """模型 Hook 串行执行管线。"""

    def __init__(self, hooks: Sequence[ModelHook] | None = None) -> None:
        """初始化模型 Hook 列表。"""
        self._hooks = list(hooks or [])  # 复制输入序列，避免外部后续修改影响内部执行顺序

    @property
    def hooks(self) -> list[ModelHook]:
        """返回当前装配的模型 Hook 列表副本。"""
        return list(self._hooks)

    async def before_model(self, request: ModelRequest, context: "ExecutionContext") -> ModelRequest:
        """按顺序执行所有 before_model Hook。"""
        current_request = request  # 保存当前请求对象，逐个 Hook 串联传递
        for hook in self._hooks:  # 按装配顺序依次执行 Hook
            try:  # 尝试执行当前 Hook
                current_request = await hook.before_model(current_request, context)  # 获取改写后的请求
            except Exception as exc:  # 捕获当前 Hook 异常
                if hook.fail_open:  # fail-open 时仅记录日志并继续后续 Hook
                    logger.warning("模型 Hook before_model 失败，已按 fail-open 跳过: hook=%s, error=%s", hook.__class__.__name__, exc)
                    continue  # 忽略当前 Hook 失败，继续执行下一个 Hook
                raise HookExecutionError("before_model", hook.__class__.__name__, exc) from exc  # fail-closed 时向上抛出稳定异常
        return current_request  # 返回所有 Hook 串行处理后的最终请求

    async def after_model(self, response: ModelResponse, context: "ExecutionContext") -> ModelResponse:
        """按顺序执行所有 after_model Hook。"""
        current_response = response  # 保存当前响应对象，逐个 Hook 串联传递
        for hook in self._hooks:  # 按装配顺序依次执行 Hook
            try:  # 尝试执行当前 Hook
                current_response = await hook.after_model(current_response, context)  # 获取改写后的响应
            except Exception as exc:  # 捕获当前 Hook 异常
                if hook.fail_open:  # fail-open 时仅记录日志并继续后续 Hook
                    logger.warning("模型 Hook after_model 失败，已按 fail-open 跳过: hook=%s, error=%s", hook.__class__.__name__, exc)
                    continue  # 忽略当前 Hook 失败，继续执行下一个 Hook
                raise HookExecutionError("after_model", hook.__class__.__name__, exc) from exc  # fail-closed 时向上抛出稳定异常
        return current_response  # 返回所有 Hook 串行处理后的最终响应


class ToolHookPipeline:
    """工具 Hook 串行执行管线。"""

    def __init__(self, hooks: Sequence[ToolHook] | None = None) -> None:
        """初始化工具 Hook 列表。"""
        self._hooks = list(hooks or [])  # 复制输入序列，避免外部修改执行顺序

    @property
    def hooks(self) -> list[ToolHook]:
        """返回当前装配的工具 Hook 列表副本。"""
        return list(self._hooks)

    async def before_tool(self, request: ToolRequest, context: "ExecutionContext") -> ToolRequest:
        """按顺序执行所有 before_tool Hook。"""
        current_request = request  # 保存当前请求对象，逐个 Hook 串联传递
        for hook in self._hooks:  # 按装配顺序依次执行 Hook
            try:  # 尝试执行当前 Hook
                current_request = await hook.before_tool(current_request, context)  # 获取改写后的请求
            except Exception as exc:  # 捕获当前 Hook 异常
                if hook.fail_open:  # fail-open 时仅记录日志并继续后续 Hook
                    logger.warning("工具 Hook before_tool 失败，已按 fail-open 跳过: hook=%s, error=%s", hook.__class__.__name__, exc)
                    continue  # 忽略当前 Hook 失败，继续执行下一个 Hook
                raise HookExecutionError("before_tool", hook.__class__.__name__, exc) from exc  # fail-closed 时向上抛出稳定异常
        return current_request  # 返回所有 Hook 串行处理后的最终请求

    async def after_tool(self, response: ToolResponse, context: "ExecutionContext") -> ToolResponse:
        """按顺序执行所有 after_tool Hook。"""
        current_response = response  # 保存当前响应对象，逐个 Hook 串联传递
        for hook in self._hooks:  # 按装配顺序依次执行 Hook
            try:  # 尝试执行当前 Hook
                current_response = await hook.after_tool(current_response, context)  # 获取改写后的响应
            except Exception as exc:  # 捕获当前 Hook 异常
                if hook.fail_open:  # fail-open 时仅记录日志并继续后续 Hook
                    logger.warning("工具 Hook after_tool 失败，已按 fail-open 跳过: hook=%s, error=%s", hook.__class__.__name__, exc)
                    continue  # 忽略当前 Hook 失败，继续执行下一个 Hook
                raise HookExecutionError("after_tool", hook.__class__.__name__, exc) from exc  # fail-closed 时向上抛出稳定异常
        return current_response  # 返回所有 Hook 串行处理后的最终响应
