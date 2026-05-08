"""Hook 请求/响应载体定义。"""

from __future__ import annotations  # 启用未来注解，避免运行时前向引用问题

from dataclasses import dataclass  # 导入数据类装饰器，定义请求/响应载体

from app.core.models.tool import Tool, ToolResult  # 导入工具抽象与工具结果模型


@dataclass
class ModelRequest:
    """模型调用请求载体。

    该载体封装调用模型前允许 Hook 改写的参数集合。
    """

    messages: list[dict]  # 已组装好的模型消息列表
    tools: list[dict] | None  # 当前轮允许模型使用的工具列表
    model: str  # 最终要调用的模型标识
    temperature: float  # 本轮调用使用的温度参数


@dataclass
class ModelResponse:
    """模型调用响应载体。

    当前响应对象在调用级 Hook 中使用：
    - text 保存已经流式输出过的完整文本聚合结果
    - tool_calls 保存本轮归并后的工具调用列表
    - usage 保存本轮 token 统计
    """

    text: str  # 已聚合的完整文本输出
    tool_calls: list[object] | None  # 工具调用列表，避免与 Runtime 形成循环依赖
    usage: object | None  # token 用量信息，保持弱类型以降低耦合


@dataclass
class ToolRequest:
    """工具调用请求载体。

    before_tool Hook 可以改写 tool_input 或替换工具实例本身。
    """

    tool_name: str  # 当前工具名称
    tool_call_id: str  # 当前工具调用唯一标识
    tool_input: dict  # 即将传给工具的输入参数
    tool: Tool  # 当前要执行的工具实例


@dataclass
class ToolResponse:
    """工具调用响应载体。

    after_tool Hook 可以基于该对象改写最终 ToolResult。
    """

    tool_name: str  # 当前工具名称
    tool_call_id: str  # 当前工具调用唯一标识
    result: ToolResult  # 当前工具执行得到的结果
