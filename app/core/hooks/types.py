"""Hook 请求/响应载体定义。"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.models.tool import Tool, ToolResult


@dataclass
class ModelRequest:
    """模型调用请求载体。

    该载体封装调用模型前允许 Hook 改写的参数集合。
    """

    messages: list[dict]
    tools: list[dict] | None
    model: str
    temperature: float


@dataclass
class ModelResponse:
    """模型调用响应载体。

    当前响应对象在调用级 Hook 中使用：
    - text 保存已经流式输出过的完整文本聚合结果
    - tool_calls 保存本轮归并后的工具调用列表
    - usage 保存本轮 token 统计
    """

    text: str
    tool_calls: list[object] | None
    usage: object | None


@dataclass
class ToolRequest:
    """工具调用请求载体。

    before_tool Hook 可以改写 tool_input 或替换工具实例本身。
    """

    tool_name: str
    tool_call_id: str
    tool_input: dict
    tool: Tool


@dataclass
class ToolResponse:
    """工具调用响应载体。

    after_tool Hook 可以基于该对象改写最终 ToolResult。
    """

    tool_name: str
    tool_call_id: str
    result: ToolResult
