"""AgentExecutionProfile 组装器。

把默认子代理定义 (DefaultSubAgentDefinition) 或自定义子代理定义
(CustomSubAgentDefinition) 组装为可供 AgentLoop 直接消费的
AgentExecutionProfile 实例。
"""

from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.core.hooks import ToolHookPipeline
from app.core.models.error import ErrorCode
from app.core.models.agent import Agent, AgentExecutionProfile, AgentPromptSource
from app.core.models.tool import Tool, ToolRegistry
from app.infra.agents.custom_sub_agent_loader import CustomSubAgentDefinition
from app.infra.agents.default_sub_agents import DefaultSubAgentDefinition
from app.infra.agents.hook_profiles import HookProfileRegistry
from app.infra.skills.catalog import SkillCatalog

# child profile 中自动过滤的主控工具
TASK_TOOL_NAME = "Task"
LIST_RESUMABLE_SUBAGENTS_TOOL_NAME = "ListResumableSubagents"
CHILD_FILTERED_TOOL_NAMES = frozenset({TASK_TOOL_NAME, LIST_RESUMABLE_SUBAGENTS_TOOL_NAME})


class SubAgentProfileBuilder:
    """把默认或自定义子代理配置组装成运行 profile。

    组装语义：
    - tools is None：不加载任何工具配置，ToolRegistry 为空
    - tools == ()：显式配置为空工具集合
    - skills is None：不加载任何 skill 配置
    - skills == ()：显式配置为空 skill 集合
    - hook_profile is None：使用空 ToolHookPipeline()
    - 包含 Task/ListResumableSubagents 的工具名自动过滤，不报错
    - 未知非主控的工具名报 INVALID_SUBAGENT_CONFIG
    - 不存在的 skill 静默跳过
    """

    def __init__(
        self,
        *,
        settings: Settings,
        runtime: object,
        tool_catalog: dict[str, Tool],
        hook_profiles: HookProfileRegistry,
        skill_catalog: SkillCatalog,
        default_prompt_root: Path,
        default_max_turns: int | None = None,
    ) -> None:
        self._settings = settings
        self._runtime = runtime
        self._tool_catalog = tool_catalog
        self._hook_profiles = hook_profiles
        self._skill_catalog = skill_catalog
        self._default_prompt_root = default_prompt_root
        self._default_max_turns = default_max_turns or settings.agent_max_turns

    def build_default_profile(self, definition: DefaultSubAgentDefinition) -> AgentExecutionProfile:
        """组装默认子代理 profile（prompt_file 相对于 default_prompt_root 解析）。"""
        prompt_path = self._resolve_default_prompt(definition.prompt_file)
        prompt = prompt_path.read_text(encoding="utf-8").strip()
        if not prompt:
            raise ValueError(f"{ErrorCode.INVALID_SUBAGENT_CONFIG.value}: 默认子代理 prompt 为空: {prompt_path}")
        return self._build_profile(
            name=definition.name,
            description=definition.description,
            prompt=prompt,
            prompt_source=AgentPromptSource(kind="file", path=str(prompt_path)),
            tools=definition.tools,
            skills=definition.skills,
            max_turns=definition.max_turns,
            hook_profile=definition.hook_profile,
            extra_system_messages=definition.extra_system_messages,
        )

    def build_custom_profile(self, definition: CustomSubAgentDefinition) -> AgentExecutionProfile:
        """组装 md 自定义子代理 profile。"""
        return self._build_profile(
            name=definition.name,
            description=definition.description,
            prompt=definition.prompt,
            prompt_source=AgentPromptSource(
                kind="file",
                path=str(definition.source_path) if definition.source_path else definition.name,
            ),
            tools=definition.tools,
            skills=definition.skills,
            max_turns=definition.max_turns,
            hook_profile=definition.hook_profile,
            extra_system_messages=(),
        )

    def _build_profile(
        self,
        *,
        name: str,
        description: str,
        prompt: str,
        prompt_source: AgentPromptSource,
        tools: tuple[str, ...] | None,
        skills: tuple[str, ...] | None,
        max_turns: int | None,
        hook_profile: str | None,
        extra_system_messages: tuple[str, ...],
    ) -> AgentExecutionProfile:
        """组装通用 child profile（model/temperature 统一复用 master 配置）。"""
        agent = Agent(
            agent_id=name,
            name=name,
            description=description,
            model=self._settings.master_agent_model,
            system_prompt=prompt,
            temperature=self._settings.master_agent_temperature,
            reasoning_effort=self._settings.master_agent_reasoning_effort,
        )
        loaded_skills, skill_messages = self._load_skill_messages(skills)
        registry = self._build_tool_registry(tools)
        return AgentExecutionProfile(
            agent_id=name,
            agent=agent,
            prompt_source=prompt_source,
            runtime=self._runtime,
            tool_registry=registry,
            tool_hook_pipeline=self._resolve_tool_hook_pipeline(hook_profile),
            max_turns=max_turns or self._default_max_turns,
            skills=loaded_skills,
            extra_system_messages=tuple(extra_system_messages) + skill_messages,
        )

    def _resolve_default_prompt(self, prompt_file: str) -> Path:
        """解析默认 prompt_file，禁止绝对路径和 app/ 前缀。"""
        if Path(prompt_file).is_absolute() or prompt_file.startswith("app/"):
            raise ValueError(
                f"{ErrorCode.INVALID_SUBAGENT_CONFIG.value}: 默认子代理 prompt_file 不能使用工程路径: {prompt_file}"
            )
        return self._default_prompt_root / prompt_file

    def _build_tool_registry(self, tool_names: tuple[str, ...] | None) -> ToolRegistry:
        """按名称组装 child 工具注册表，自动过滤主控工具。"""
        registry = ToolRegistry()
        if tool_names is None:
            return registry
        for tool_name in tool_names:
            if tool_name in CHILD_FILTERED_TOOL_NAMES:
                continue
            tool = self._tool_catalog.get(tool_name)
            if tool is None:
                raise ValueError(f"{ErrorCode.INVALID_SUBAGENT_CONFIG.value}: 未知工具: {tool_name}")
            registry.register(tool)
        return registry

    def _load_skill_messages(self, skill_names: tuple[str, ...] | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """加载 skill 内容并转成系统消息格式，缺失的 skill 静默跳过。"""
        if skill_names is None:
            return (), ()
        loaded_names: list[str] = []
        messages: list[str] = []
        for skill_name in skill_names:
            try:
                document = self._skill_catalog.get(skill_name)
            except ValueError:
                continue
            loaded_names.append(document.name)
            messages.append(f"<skill name=\"{document.name}\">\n{document.content}\n</skill>")
        return tuple(loaded_names), tuple(messages)

    def _resolve_tool_hook_pipeline(self, hook_profile: str | None) -> ToolHookPipeline:
        """解析 Hook profile，None 返回空管线。"""
        if hook_profile is None:
            return ToolHookPipeline()
        return self._hook_profiles.get(hook_profile)
