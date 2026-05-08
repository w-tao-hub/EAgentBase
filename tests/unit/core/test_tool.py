"""Tool、ToolResult、ToolRegistry 单元测试。"""

import pytest  # 导入 pytest 测试框架

from app.core.models.tool import Tool, ToolRegistry, ToolResult  # 导入被测对象


class FakeTool(Tool):
    """测试用的假工具实现。"""

    @property
    def name(self) -> str:
        """返回工具名称。"""
        return "fake_tool"

    @property
    def description(self) -> str:
        """返回工具描述。"""
        return "A fake tool for testing"

    @property
    def input_schema(self) -> dict:
        """返回输入参数 schema。"""
        return {
            "type": "object",  # JSON Schema 对象类型
            "properties": {
                "query": {"type": "string"},  # query 参数为字符串
            },
            "required": ["query"],  # query 为必填参数
        }

    async def call(self, input: dict) -> ToolResult:
        """模拟工具调用。"""
        # 返回固定格式的成功结果
        return ToolResult(content=f"Result for: {input.get('query', '')}")


class ReadOnlyTool(Tool):
    """测试用的只读工具。"""

    @property
    def name(self) -> str:
        """返回工具名称。"""
        return "read_only_tool"

    @property
    def description(self) -> str:
        """返回工具描述。"""
        return "A read-only tool"

    @property
    def input_schema(self) -> dict:
        """返回输入参数 schema。"""
        return {"type": "object", "properties": {}}  # 空参数对象

    async def call(self, input: dict) -> ToolResult:
        """模拟工具调用。"""
        return ToolResult(content="readonly result")

    def is_read_only(self) -> bool:
        """覆盖为只读工具。"""
        # 返回 True 表示这是只读工具
        return True


class TestToolResult:
    """ToolResult 测试类。"""

    def test_tool_result_success(self):
        """测试成功结果创建。"""
        # 创建成功结果，is_error 默认为 False
        result = ToolResult(content="success output")
        # 验证内容正确
        assert result.content == "success output"
        # 验证不是错误结果
        assert result.is_error is False

    def test_tool_result_error(self):
        """测试错误结果创建。"""
        # 创建错误结果，显式设置 is_error=True
        result = ToolResult(content="error message", is_error=True)
        # 验证内容正确
        assert result.content == "error message"
        # 验证是错误结果
        assert result.is_error is True


class TestToolABC:
    """Tool ABC 测试类。"""

    def test_tool_is_abstract(self):
        """测试 Tool 是抽象类，不能直接实例化。"""
        # 尝试直接实例化抽象类应该抛出 TypeError
        with pytest.raises(TypeError):
            Tool()  # type: ignore

    def test_fake_tool_properties(self):
        """测试假工具属性。"""
        # 创建假工具实例
        tool = FakeTool()
        # 验证名称
        assert tool.name == "fake_tool"
        # 验证描述
        assert tool.description == "A fake tool for testing"
        # 验证 schema 结构
        assert tool.input_schema["type"] == "object"
        assert "query" in tool.input_schema["properties"]

    def test_tool_is_read_only_default(self):
        """测试默认 is_read_only 返回 False（fail-closed）。"""
        # 创建假工具实例
        tool = FakeTool()
        # 默认实现返回 False
        assert tool.is_read_only() is False

    def test_read_only_tool_override(self):
        """测试只读工具覆盖 is_read_only。"""
        # 创建只读工具实例
        tool = ReadOnlyTool()
        # 覆盖后返回 True
        assert tool.is_read_only() is True


class TestToolRegistry:
    """ToolRegistry 测试类。"""

    def test_register_tool(self):
        """测试注册工具。"""
        # 创建注册表实例
        registry = ToolRegistry()
        # 创建假工具
        tool = FakeTool()
        # 注册工具
        registry.register(tool)
        # 验证注册成功
        assert len(registry) == 1
        assert "fake_tool" in registry

    def test_register_duplicate_raises(self):
        """测试重复注册抛出异常。"""
        # 创建注册表实例
        registry = ToolRegistry()
        # 创建并注册第一个工具
        tool = FakeTool()
        registry.register(tool)
        # 尝试重复注册同名工具应该抛出 ValueError
        with pytest.raises(ValueError, match="已注册"):
            registry.register(tool)

    def test_get_existing_tool(self):
        """测试获取已注册工具。"""
        # 创建注册表实例
        registry = ToolRegistry()
        # 创建并注册工具
        tool = FakeTool()
        registry.register(tool)
        # 获取工具
        found = registry.get("fake_tool")
        # 验证找到的工具
        assert found is not None
        assert found.name == "fake_tool"

    def test_get_nonexistent_tool(self):
        """测试获取不存在的工具返回 None。"""
        # 创建注册表实例
        registry = ToolRegistry()
        # 获取不存在的工具
        found = registry.get("nonexistent")
        # 验证返回 None
        assert found is None

    def test_to_llm_tools_empty(self):
        """测试空注册表转换为空列表。"""
        # 创建空注册表
        registry = ToolRegistry()
        # 转换为 LLM 格式
        llm_tools = registry.to_llm_tools()
        # 验证为空列表
        assert llm_tools == []

    def test_to_llm_tools_format(self):
        """测试 to_llm_tools 返回正确格式。"""
        # 创建注册表实例
        registry = ToolRegistry()
        # 创建并注册工具
        tool = FakeTool()
        registry.register(tool)
        # 转换为 LLM 格式
        llm_tools = registry.to_llm_tools()
        # 验证列表长度
        assert len(llm_tools) == 1
        # 验证第一个工具结构
        tool_def = llm_tools[0]
        # 验证类型字段
        assert tool_def["type"] == "function"
        # 验证 function 对象存在
        assert "function" in tool_def
        # 验证函数名称
        assert tool_def["function"]["name"] == "fake_tool"
        # 验证函数描述
        assert tool_def["function"]["description"] == "A fake tool for testing"
        # 验证参数 schema
        assert "parameters" in tool_def["function"]
        assert tool_def["function"]["parameters"]["type"] == "object"

    def test_to_llm_tools_multiple_tools(self):
        """测试多个工具转换。"""
        # 创建注册表实例
        registry = ToolRegistry()
        # 注册两个工具
        registry.register(FakeTool())
        registry.register(ReadOnlyTool())
        # 转换为 LLM 格式
        llm_tools = registry.to_llm_tools()
        # 验证列表长度
        assert len(llm_tools) == 2
        # 提取所有工具名称
        names = [t["function"]["name"] for t in llm_tools]
        # 验证包含两个工具
        assert "fake_tool" in names
        assert "read_only_tool" in names

    def test_list_tools(self):
        """测试列出工具名称。"""
        # 创建注册表实例
        registry = ToolRegistry()
        # 注册两个工具
        registry.register(FakeTool())
        registry.register(ReadOnlyTool())
        # 获取工具名称列表
        names = registry.list_tools()
        # 验证列表内容
        assert sorted(names) == ["fake_tool", "read_only_tool"]

    def test_unregister(self):
        """测试注销工具。"""
        # 创建注册表实例
        registry = ToolRegistry()
        # 注册工具
        registry.register(FakeTool())
        # 验证已注册
        assert "fake_tool" in registry
        # 注销工具
        result = registry.unregister("fake_tool")
        # 验证注销成功
        assert result is True
        # 验证已移除
        assert "fake_tool" not in registry
        assert len(registry) == 0

    def test_unregister_nonexistent(self):
        """测试注销不存在的工具返回 False。"""
        # 创建注册表实例
        registry = ToolRegistry()
        # 尝试注销不存在的工具
        result = registry.unregister("nonexistent")
        # 验证返回 False
        assert result is False

    def test_clear(self):
        """测试清空注册表。"""
        # 创建注册表实例
        registry = ToolRegistry()
        # 注册多个工具
        registry.register(FakeTool())
        registry.register(ReadOnlyTool())
        # 验证已注册
        assert len(registry) == 2
        # 清空注册表
        registry.clear()
        # 验证为空
        assert len(registry) == 0

    def test_contains(self):
        """测试 __contains__ 操作符。"""
        # 创建注册表实例
        registry = ToolRegistry()
        # 注册工具
        registry.register(FakeTool())
        # 验证包含检查
        assert "fake_tool" in registry
        assert "nonexistent" not in registry
