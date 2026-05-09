"""MCP 适配器单元测试。"""

from __future__ import annotations

import json

import pytest

from app.core.models.agent import Agent
from app.core.models.execution_context import ExecutionContext
from app.infra.tools.mcp_adapter import MCPToolAdapter


class FakeTextContent:  # 定义文本内容块替身。
    """模拟 MCP 文本内容块。"""

    def __init__(self, text: str) -> None:  # 定义初始化方法。
        """保存文本内容。"""
        self.text = text  # 保存文本字段，供适配器读取。


class FakeCallToolResult:  # 定义工具调用结果替身。
    """模拟 MCP SDK 的 CallToolResult。"""

    def __init__(  # 定义初始化方法。
        self,  # 声明实例自身参数。
        content: list[object] | None = None,
        structured_content: dict | None = None,
        is_error: bool = False,
    ) -> None:
        """保存工具调用结果字段。"""
        self.content = content or []  # 保存内容块列表，默认空列表。
        self.structuredContent = structured_content  # 保存结构化内容字段，模拟官方字段名。
        self.isError = is_error  # 保存错误标记字段，模拟官方字段名。


class FakeClientSession:  # 定义客户端会话替身。
    """模拟 MCP ClientSession。"""

    def __init__(self, result: FakeCallToolResult | None = None, error: Exception | None = None) -> None:  # 定义初始化方法。
        """保存预设结果或异常。"""
        self._result = result  # 保存预设结果。
        self._error = error  # 保存预设异常。
        self.last_call: dict | None = None  # 记录最后一次调用参数。

    async def call_tool(self, name: str, arguments: dict) -> FakeCallToolResult:  # 定义异步调用方法。
        """返回预设结果，或抛出预设异常。"""
        self.last_call = {"name": name, "arguments": arguments}  # 记录最后一次调用参数。
        if self._error is not None:  # 判断是否需要抛出异常。
            raise self._error  # 抛出预设异常。
        return self._result or FakeCallToolResult()  # 返回预设结果，若为空则返回默认空结果。


@pytest.fixture  # 声明 pytest 夹具。
def mock_context() -> ExecutionContext:  # 定义执行上下文夹具。
    """构造测试用执行上下文。"""
    agent = Agent(  # 创建测试用 Agent。
        agent_id="test-agent",  # 设置智能体标识。
        name="Test Agent",  # 设置智能体名称。
        model="gpt-4.1-mini",  # 设置模型名称。
        system_prompt="You are helpful.",  # 设置系统提示词。
        temperature=0.2,  # 设置温度参数。
    )  # 结束 Agent 构造。
    return ExecutionContext(  # 返回执行上下文实例。
        run_id="test-run-1",  # 设置运行标识。
        session_id="test-session-1",  # 设置会话标识。
        metadata=None,  # 设置元数据为空。
        agent=agent,  # 注入测试用 Agent。
    )  # 结束上下文构造。


class TestMCPToolAdapter:  # 定义 MCP 适配器测试类。
    """验证 MCP 工具适配行为。"""

    def test_name_is_exposed_with_mcp_prefix(self) -> None:  # 定义工具名映射测试。
        """验证对外暴露名固定为 mcp_<tool_name>。"""
        adapter = MCPToolAdapter(  # 创建适配器实例。
            server_id="server-a",  # 设置服务标识。
            remote_tool_name="fetch",  # 设置远端工具原始名称。
            description="Fetch remote content",  # 设置工具描述。
            input_schema={"type": "object"},  # 设置输入参数模式。
            session=FakeClientSession(),  # 注入会话替身。
        )  # 结束适配器构造。

        assert adapter.name == "mcp_fetch"  # 断言对外名称已添加 mcp_ 前缀。

    @pytest.mark.asyncio  # 标记为异步测试。
    async def test_call_formats_text_and_structured_content(self, mock_context: ExecutionContext) -> None:  # 定义结果格式化测试。
        """验证工具结果会收敛为文本，并追加结构化内容。"""
        session = FakeClientSession(  # 创建会话替身。
            result=FakeCallToolResult(  # 配置预设工具结果。
                content=[FakeTextContent("first line"), FakeTextContent("second line")],  # 配置文本内容块。
                structured_content={"answer": 42},  # 配置结构化内容。
                is_error=False,  # 标记结果为非错误。
            )  # 结束结果构造。
        )  # 结束会话构造。
        adapter = MCPToolAdapter(  # 创建适配器实例。
            server_id="server-a",  # 设置服务标识。
            remote_tool_name="fetch",  # 设置远端工具原始名称。
            description="Fetch remote content",  # 设置工具描述。
            input_schema={"type": "object"},  # 设置输入参数模式。
            session=session,  # 注入会话替身。
        )  # 结束适配器构造。

        result = await adapter.call({"url": "https://example.com"}, mock_context)  # 调用工具适配器。

        assert session.last_call == {"name": "fetch", "arguments": {"url": "https://example.com"}}  # 断言远端调用使用原始工具名。
        assert result.is_error is False  # 断言结果错误标记为 False。
        assert result.content == "first line\nsecond line\n\nstructuredContent:\n" + json.dumps({"answer": 42}, ensure_ascii=False, indent=2)  # 断言文本与结构化内容均被序列化。

    @pytest.mark.asyncio  # 标记为异步测试。
    async def test_call_returns_error_result_when_session_raises(self, mock_context: ExecutionContext) -> None:  # 定义异常收敛测试。
        """验证远端调用异常会收敛为错误 ToolResult。"""
        adapter = MCPToolAdapter(  # 创建适配器实例。
            server_id="server-a",  # 设置服务标识。
            remote_tool_name="fetch",  # 设置远端工具原始名称。
            description="Fetch remote content",  # 设置工具描述。
            input_schema={"type": "object"},  # 设置输入参数模式。
            session=FakeClientSession(error=RuntimeError("network down")),  # 注入会抛错的会话替身。
        )  # 结束适配器构造。

        result = await adapter.call({"url": "https://example.com"}, mock_context)  # 调用工具适配器。

        assert result.is_error is True  # 断言结果被标记为错误。
        assert "network down" in result.content  # 断言错误信息被保留在结果文本中。
