"""默认子代理声明式配置。"""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class DefaultSubAgentDefinition:
    """默认子代理配置项，只保存可快速调整的静态字段。"""

    name: str  # 子代理标识名称，同时作为 agent_id 使用
    description: str  # 子代理功能描述，供系统提醒和文档使用
    prompt_file: str  # prompt 模板文件名，相对于默认子代理包目录解析
    tools: tuple[str, ...] | None = None  # 需要加载的工具名称列表，None 表示不加载任何工具
    skills: tuple[str, ...] | None = None  # 需要注入的 skill 名称列表，None 表示不加载任何 skill
    max_turns: int | None = None  # 最大对话轮数，None 表示使用全局默认值
    hook_profile: str | None = None  # 引用的 Hook profile 名称，None 表示不加载任何 Hook 管线
    extra_system_messages: tuple[str, ...] = ()  # 额外的系统级消息，会在系统提示词之后追加

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
        hook_profile=None,
        extra_system_messages=(),
    ),
)
