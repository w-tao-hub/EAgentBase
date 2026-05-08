"""Hook 基类定义。"""

from __future__ import annotations  # 启用未来注解，避免运行时前向引用问题

from typing import TYPE_CHECKING  # 导入类型提示工具

from app.core.hooks.types import ModelRequest, ModelResponse, ToolRequest, ToolResponse  # 导入 Hook 载体

if TYPE_CHECKING:  # 仅在类型检查阶段导入，避免循环依赖
    from app.core.models.execution_context import ExecutionContext  # 导入执行上下文类型


class ModelHook:
    """模型调用 Hook 基类。

    默认实现为 no-op，子类按需覆盖 before_model 或 after_model。
    """

    def __init__(self, fail_open: bool = False) -> None:
        """初始化 Hook 失败策略。

        Args:
            fail_open: 为 True 时，Hook 抛错只记录日志并跳过
        """
        self._fail_open = fail_open  # 保存失败策略，供 Pipeline 判断是否吞错

    @property
    def fail_open(self) -> bool:
        """返回当前 Hook 是否采用 fail-open 策略。"""
        return self._fail_open

    async def before_model(self, request: ModelRequest, context: "ExecutionContext") -> ModelRequest:
        """模型调用前处理请求。

        默认直接透传请求，不做任何改写。
        """
        return request

    async def after_model(self, response: ModelResponse, context: "ExecutionContext") -> ModelResponse:
        """模型调用后处理响应。

        默认直接透传响应，不做任何改写。
        """
        return response


class ToolHook:
    """工具调用 Hook 基类。

    默认实现为 no-op，子类按需覆盖 before_tool 或 after_tool。
    """

    def __init__(self, fail_open: bool = False) -> None:
        """初始化 Hook 失败策略。

        Args:
            fail_open: 为 True 时，Hook 抛错只记录日志并跳过
        """
        self._fail_open = fail_open  # 保存失败策略，供 Pipeline 判断是否吞错

    @property
    def fail_open(self) -> bool:
        """返回当前 Hook 是否采用 fail-open 策略。"""
        return self._fail_open

    async def before_tool(self, request: ToolRequest, context: "ExecutionContext") -> ToolRequest:
        """工具调用前处理请求。

        默认直接透传请求，不做任何改写。
        """
        return request

    async def after_tool(self, response: ToolResponse, context: "ExecutionContext") -> ToolResponse:
        """工具调用后处理响应。

        默认直接透传响应，不做任何改写。
        """
        return response
