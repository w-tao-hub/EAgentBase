"""默认子代理声明式配置。"""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class DefaultSubAgentDefinition:
    """默认子代理配置项，只保存可快速调整的静态字段。"""

    name: str
    description: str
    prompt_file: str
    tools: tuple[str, ...] | None = None
    skills: tuple[str, ...] | None = None
    max_turns: int | None = None
    tool_hook_profiles: tuple[str, ...] | None = None
    model_hook_profiles: tuple[str, ...] | None = None
    extra_system_messages: tuple[str, ...] = ()
    mount_master_agents: tuple[str, ...] | None = None

    def with_overrides(self, **changes: object) -> "DefaultSubAgentDefinition":
        """测试和局部配置复用的不可变覆盖方法。"""
        return replace(self, **changes)


DEFAULT_SUB_AGENT_DEFINITIONS: tuple[DefaultSubAgentDefinition, ...] = (
    DefaultSubAgentDefinition(
        name="Worker",
        description="通用子代理，可执行主代理分派的复杂任务",
        prompt_file="worker.md",
        tools=(),
        skills=None,
        max_turns=100,
        tool_hook_profiles=None,
        model_hook_profiles=None,
        extra_system_messages=(),
    ),
)
