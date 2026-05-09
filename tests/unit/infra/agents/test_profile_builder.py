"""SubAgentProfileBuilder 单元测试。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.core.models.tool import Tool, ToolRegistry
from app.infra.agents.profile_builder import (
    CHILD_FILTERED_TOOL_NAMES,
    LIST_RESUMABLE_SUBAGENTS_TOOL_NAME,
    SubAgentProfileBuilder,
    TASK_TOOL_NAME,
)


class TestChildFilteredToolNames:
    """CHILD_FILTERED_TOOL_NAMES 常量验证。"""

    def test_contains_task(self) -> None:
        """验证 CHILD_FILTERED_TOOL_NAMES 包含 Task。"""
        assert TASK_TOOL_NAME in CHILD_FILTERED_TOOL_NAMES

    def test_contains_list_resumable_subagents(self) -> None:
        """验证 CHILD_FILTERED_TOOL_NAMES 包含 ListResumableSubagents。"""
        assert LIST_RESUMABLE_SUBAGENTS_TOOL_NAME in CHILD_FILTERED_TOOL_NAMES

    def test_exact_count(self) -> None:
        """验证 CHILD_FILTERED_TOOL_NAMES 的元素数量为 2。"""
        assert len(CHILD_FILTERED_TOOL_NAMES) == 2


class MockTool(Tool):
    """测试用最小工具实现。"""

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return ""

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def call(self, input: dict, context) -> None:
        return None


class TestBuildToolRegistry:
    """_build_tool_registry 工具过滤行为验证。"""

    def _make_builder(self) -> SubAgentProfileBuilder:
        """创建最小配置的 SubAgentProfileBuilder 实例。"""
        settings = MagicMock()
        settings.master_agent_model = "gpt-4"
        settings.master_agent_temperature = 0.0
        settings.master_agent_reasoning_effort = None
        settings.agent_max_turns = 10

        tool_catalog = {
            "search": MockTool("search"),
            "read_file": MockTool("read_file"),
        }

        return SubAgentProfileBuilder(
            settings=settings,
            runtime=MagicMock(),
            tool_catalog=tool_catalog,
            hook_profiles=MagicMock(),
            skill_catalog=MagicMock(),
            default_prompt_root=Path("/tmp"),
            default_max_turns=10,
        )

    def test_filtered_tools_are_skipped(self) -> None:
        """验证 Task 和 ListResumableSubagents 会被自动过滤。"""
        builder = self._make_builder()
        registry = builder._build_tool_registry(
            ("search", TASK_TOOL_NAME, "read_file", LIST_RESUMABLE_SUBAGENTS_TOOL_NAME)
        )
        tools = registry.list_tools()
        assert "search" in tools
        assert "read_file" in tools
        assert TASK_TOOL_NAME not in tools
        assert LIST_RESUMABLE_SUBAGENTS_TOOL_NAME not in tools

    def test_none_returns_empty_registry(self) -> None:
        """验证 tool_names 为 None 时返回空注册表。"""
        builder = self._make_builder()
        registry = builder._build_tool_registry(None)
        assert registry.list_tools() == []

    def test_unknown_tool_raises_error(self) -> None:
        """验证未知工具名称会抛出 ValueError。"""
        builder = self._make_builder()
        with pytest.raises(ValueError, match="INVALID_SUBAGENT_CONFIG"):
            builder._build_tool_registry(("nonexistent_tool",))
