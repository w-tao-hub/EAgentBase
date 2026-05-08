"""Agent profile 相关模型测试。"""

from __future__ import annotations

from app.core.hooks import ToolHookPipeline
from app.core.models.agent import Agent, AgentExecutionProfile, AgentPromptSource
from app.core.models.tool import ToolRegistry
from tests.fakes import FakeAgentRuntime


def test_agent_execution_profile_keeps_runtime_dependencies_outside_agent() -> None:
    """测试 profile 承载运行依赖，而 Agent 仍只保存模型元信息。"""
    agent = Agent(
        agent_id="Plan",
        name="Plan",
        model="deepseek/deepseek-v4-flash",
        system_prompt="你是计划软件架构代理。",
        temperature=0.2,
        reasoning_effort="high",
    )
    runtime = FakeAgentRuntime()
    registry = ToolRegistry()
    hook_pipeline = ToolHookPipeline()

    profile = AgentExecutionProfile(
        agent_id="Plan",
        agent=agent,
        prompt_source=AgentPromptSource(kind="file", path="plan.md"),
        runtime=runtime,
        tool_registry=registry,
        tool_hook_pipeline=hook_pipeline,
        max_turns=8,
        skills=("code-review",),
        extra_system_messages=("额外规则",),
    )

    assert profile.agent is agent
    assert profile.runtime is runtime
    assert profile.tool_registry is registry
    assert profile.tool_hook_pipeline is hook_pipeline
    assert profile.max_turns == 8
    assert profile.skills == ("code-review",)
    assert profile.extra_system_messages == ("额外规则",)


def test_agent_prompt_source_only_accepts_file_kind() -> None:
    """测试 prompt source 本期只允许文件来源。"""
    source = AgentPromptSource(kind="file", path="plan.md")
    assert source.kind == "file"
    assert source.path == "plan.md"
