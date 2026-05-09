"""工具模型定义。

定义 Tool 抽象基类、ToolResult 结果模型和 ToolRegistry 注册表，
为智能体提供统一的工具调用接口。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.models.execution_context import ExecutionContext
    from app.core.models.stored_message import StoredMessage


@dataclass
class ToolResultMeta:
    """工具内部元数据。

    仅供编排层与持久化层消费，不进入用户可见正文协议。
    """

    task_child_id: str | None = None


@dataclass
class ToolResult:
    """工具执行结果。

    封装工具调用的输出内容和错误状态，统一返回格式。
    """

    content: str
    is_error: bool = False
    stored_message: "StoredMessage | None" = None
    meta: ToolResultMeta | None = None


@dataclass
class ToolExecuteItem:
    """待执行工具项数据类。

    用于存储工具调用的完整信息，替代 tuple[str, str, dict, Tool]。
    """
    tool_call_id: str
    tool_name: str
    tool_input: dict
    tool: "Tool"


@dataclass
class ToolExecuteResult:
    """工具执行结果数据类。

    用于存储工具执行的返回结果，替代 tuple[str, str, ToolResult]。
    """
    tool_call_id: str
    tool_name: str
    result: ToolResult


class Tool(ABC):
    """工具抽象基类。

    所有具体工具必须继承此类，实现 name、description、input_schema
    属性和 call 方法，以及可选的 is_read_only 方法。

    遵循 fail-closed 原则，is_read_only 默认返回 False，
    只有明确声明为只读的工具才返回 True。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """工具标识符，用于注册和查找。"""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """工具描述，用于向 LLM 说明工具用途。"""
        ...

    @property
    @abstractmethod
    def input_schema(self) -> dict:
        """JSON Schema 格式的输入参数定义。

        描述工具需要的参数结构、类型和约束，供 LLM 生成正确调用。
        """
        ...

    @abstractmethod
    async def call(self, input: dict, context: ExecutionContext) -> ToolResult:
        """异步执行工具。

        Args:
            input: 工具输入参数，必须符合 input_schema 定义
            context: 执行上下文，包含 run_id、session_id、metadata、agent 等信息

        Returns:
            ToolResult: 工具执行结果
        """
        ...

    def is_read_only(self) -> bool:
        """判断工具是否为只读操作。

        只读工具不会修改系统状态，可以安全地重试或预执行。
        默认返回 False（fail-closed 原则），子类可覆盖。

        Returns:
            bool: True 表示只读工具，False 表示可能修改状态
        """
        return False


class ToolRegistry:
    """工具注册表。

    管理 Tool 实例的注册、查找和序列化，支持转换为 LLM API 格式。
    """

    def __init__(self) -> None:
        """初始化空注册表。"""
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册工具实例。

        Args:
            tool: 要注册的工具实例

        Raises:
            ValueError: 如果同名工具已存在
        """
        if tool.name in self._tools:
            raise ValueError(f"工具 '{tool.name}' 已注册")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        """按名称查找工具。

        Args:
            name: 工具名称

        Returns:
            Tool | None: 找到的工具实例，不存在则返回 None
        """
        return self._tools.get(name)

    def to_llm_tools(self) -> List[dict]:
        """转换为 LLM API 的 tools 参数格式。

        将注册的所有工具转换为 OpenAI/Anthropic 等 LLM API
        所需的工具定义格式。

        Returns:
            list[dict]: 工具定义列表，每个元素包含 type、function 等字段
        """
        result: List[dict] = []
        for tool in self._tools.values():
            tool_def = {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            }
            result.append(tool_def)
        return result

    def list_tools(self) -> List[str]:
        """列出所有已注册工具名称。

        Returns:
            list[str]: 工具名称列表
        """
        return list(self._tools.keys())

    def unregister(self, name: str) -> bool:
        """注销指定名称的工具。

        Args:
            name: 要注销的工具名称

        Returns:
            bool: 成功注销返回 True，工具不存在返回 False
        """
        if name in self._tools:
            del self._tools[name]
            return True
        return False

    def clear(self) -> None:
        """清空所有注册的工具。"""
        self._tools = {}

    def __len__(self) -> int:
        """返回已注册工具数量。"""
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        """检查是否包含指定名称的工具。"""
        return name in self._tools
