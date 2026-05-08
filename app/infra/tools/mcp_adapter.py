"""MCP 工具适配器实现。"""

from __future__ import annotations  # 启用未来注解，避免前向引用问题。

import json  # 导入 JSON 模块，用于结构化结果序列化。
from typing import TYPE_CHECKING, Any  # 导入类型提示，便于约束会话协议和通用输入。

from app.core.models.tool import Tool, ToolResult  # 导入工具抽象与统一结果模型。

if TYPE_CHECKING:  # 仅在类型检查时导入，避免运行时引入循环依赖。
    from app.core.models.execution_context import ExecutionContext  # 导入执行上下文类型。


class MCPToolAdapter(Tool):  # 定义 MCP 工具适配器。
    """把远端 MCP tool 适配为项目内 Tool。"""

    def __init__(  # 定义初始化方法。
        self,  # 声明实例自身参数。
        server_id: str,  # 声明服务标识参数。
        remote_tool_name: str,  # 声明远端工具原始名称参数。
        description: str,  # 声明工具描述参数。
        input_schema: dict[str, Any],  # 声明输入模式参数。
        session: Any,  # 声明 MCP 会话或会话代理参数。
    ) -> None:
        """保存远端工具元数据与调用入口。"""
        self._server_id = server_id  # 保存服务标识，便于调试和报错定位。
        self._remote_tool_name = remote_tool_name  # 保存远端工具原始名称，供实际调用使用。
        self._description = description  # 保存工具描述文本。
        self._input_schema = input_schema  # 保存输入参数模式。
        self._session = session  # 保存会话对象或代理对象。

    @property  # 声明名称属性。
    def name(self) -> str:  # 定义名称属性。
        """返回统一前缀后的工具名。"""
        return f"mcp_{self._remote_tool_name}"  # 返回固定映射规则下的对外工具名称。

    @property  # 声明描述属性。
    def description(self) -> str:  # 定义描述属性。
        """返回 MCP 工具描述。"""
        return self._description  # 返回初始化时传入的工具描述。

    @property  # 声明输入模式属性。
    def input_schema(self) -> dict[str, Any]:  # 定义输入模式属性。
        """返回 MCP 工具输入参数模式。"""
        return self._input_schema  # 返回初始化时传入的输入参数模式。

    def is_read_only(self) -> bool:  # 定义只读判定方法。
        """当前默认按非只读处理，遵循 fail-closed 原则。"""
        return False  # 返回 False，避免误判远端副作用能力。

    async def call(self, input: dict, context: "ExecutionContext") -> ToolResult:  # 定义异步调用方法。
        """调用远端 MCP 工具，并把结果收敛为 ToolResult。"""
        _ = context  # 当前实现暂不消费执行上下文，但保留签名以兼容统一接口。
        try:  # 尝试调用远端 MCP 工具。
            raw_result = await self._session.call_tool(self._remote_tool_name, arguments=input)  # 使用远端原始工具名发起调用，而不是本地带前缀名称。
        except Exception as exc:  # 捕获远端调用异常。
            return ToolResult(content=f"MCP 工具调用失败: server={self._server_id}, tool={self._remote_tool_name}, error={exc}", is_error=True)  # 把异常收敛为错误结果。

        content_text = self._stringify_content_blocks(getattr(raw_result, "content", []))  # 提取并拼接内容块文本。
        structured_content = getattr(raw_result, "structuredContent", None)  # 读取结构化内容字段。
        if structured_content is not None:  # 判断是否存在结构化内容。
            structured_text = json.dumps(structured_content, ensure_ascii=False, indent=2)  # 序列化结构化内容，便于直接展示。
            if content_text:  # 判断当前是否已有文本内容。
                content_text = f"{content_text}\n\nstructuredContent:\n{structured_text}"  # 将结构化内容追加到文本结果尾部。
            else:  # 处理无文本块、只有结构化内容的情况。
                content_text = f"structuredContent:\n{structured_text}"  # 直接使用结构化内容作为结果文本。

        if not content_text:  # 判断是否仍然为空字符串。
            content_text = "MCP 工具未返回可展示内容"  # 提供稳定兜底文本，避免 tool 消息为空。

        is_error = bool(getattr(raw_result, "isError", False))  # 读取远端错误标记，并兼容缺省场景。
        return ToolResult(content=content_text, is_error=is_error)  # 返回统一结果对象。

    def _stringify_content_blocks(self, blocks: list[Any]) -> str:  # 定义内容块转文本辅助方法。
        """把 MCP 返回的内容块列表转成单个文本结果。"""
        text_parts: list[str] = []  # 初始化文本片段列表。
        for block in blocks:  # 遍历所有内容块。
            text_value = getattr(block, "text", None)  # 优先读取 text 字段。
            if isinstance(text_value, str) and text_value:  # 判断 text 字段是否为非空字符串。
                text_parts.append(text_value)  # 记录文本块内容。
                continue  # 当前内容块已处理完成，继续下一个。
            if isinstance(block, str) and block:  # 兼容内容块直接为字符串的测试替身场景。
                text_parts.append(block)  # 记录字符串内容。
                continue  # 当前内容块已处理完成，继续下一个。
            serialized_block = self._safe_serialize_block(block)  # 尝试把其他类型内容块转成可读文本。
            if serialized_block:  # 判断序列化结果是否非空。
                text_parts.append(serialized_block)  # 记录序列化后的文本表示。
        return "\n".join(text_parts)  # 用换行拼接所有文本片段。

    def _safe_serialize_block(self, block: Any) -> str:  # 定义安全序列化辅助方法。
        """把非文本内容块转成尽量稳定的字符串。"""
        try:  # 尝试优先序列化对象字典。
            if hasattr(block, "__dict__"):  # 判断对象是否具备实例字典。
                return json.dumps(block.__dict__, ensure_ascii=False, default=str)  # 序列化实例字典，尽量保留字段信息。
            return json.dumps(block, ensure_ascii=False, default=str)  # 否则直接序列化对象本身。
        except TypeError:  # 捕获不可 JSON 序列化的场景。
            return str(block)  # 回退为普通字符串表示。
