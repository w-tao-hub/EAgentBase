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

# 主控工具名称常量，child profile 中自动过滤这些工具
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
        """保存 profile 组装所需的所有依赖。

        Args:
            settings: 应用配置，child agent 的 model/temperature/reasoning_effort 从此读取
            runtime: Agent 运行时实例（单例复用）
            tool_catalog: 系统全局工具目录，按名称索引
            hook_profiles: Hook profile 注册表
            skill_catalog: Skill 文档索引
            default_prompt_root: 默认子代理 prompt 文件的根目录
            default_max_turns: 子代理默认最大轮数，None 时使用 settings.agent_max_turns
        """
        self._settings = settings  # 保存应用配置
        self._runtime = runtime  # 保存运行时实例
        self._tool_catalog = tool_catalog  # 保存全局工具目录
        self._hook_profiles = hook_profiles  # 保存 Hook profile 注册表
        self._skill_catalog = skill_catalog  # 保存 Skill 索引
        self._default_prompt_root = default_prompt_root  # 保存默认 prompt 根目录
        self._default_max_turns = default_max_turns or settings.agent_max_turns  # 默认最大轮数

    def build_default_profile(self, definition: DefaultSubAgentDefinition) -> AgentExecutionProfile:
        """组装 Python definition 默认子代理 profile。

        默认 prompt_file 相对于 default_prompt_root 解析，
        不允许使用绝对路径或 app/ 前缀路径。

        Args:
            definition: 默认子代理声明式定义

        Returns:
            组装完成的 AgentExecutionProfile

        Raises:
            ValueError: prompt_file 使用了绝对路径或 app/ 前缀、prompt 文件为空
        """
        prompt_path = self._resolve_default_prompt(definition.prompt_file)  # 解析 prompt 文件路径
        prompt = prompt_path.read_text(encoding="utf-8").strip()  # 读取并清理 prompt 内容
        if not prompt:  # prompt 文件为空时报错
            raise ValueError(f"{ErrorCode.INVALID_SUBAGENT_CONFIG.value}: 默认子代理 prompt 为空: {prompt_path}")
        return self._build_profile(
            name=definition.name,
            description=definition.description,
            prompt=prompt,
            prompt_source=AgentPromptSource(kind="file", path=str(prompt_path)),  # 记录 prompt 来源
            tools=definition.tools,
            skills=definition.skills,
            max_turns=definition.max_turns,
            hook_profile=definition.hook_profile,
            extra_system_messages=definition.extra_system_messages,
        )

    def build_custom_profile(self, definition: CustomSubAgentDefinition) -> AgentExecutionProfile:
        """组装 md 自定义子代理 profile。

        prompt 直接来自 md 正文，prompt_source 记录源文件路径。

        Args:
            definition: 从 md 解析出的自定义子代理定义

        Returns:
            组装完成的 AgentExecutionProfile
        """
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
            extra_system_messages=(),  # 自定义子代理不支持预设额外系统消息
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
        """组装通用 child profile。

        所有 child agent 的 model、temperature、reasoning_effort 统一
        从 Settings 的 master 字段读取，保证模型配置一致性。

        Args:
            name: 子代理名称，同时作为 agent_id
            description: 子代理描述
            prompt: 系统提示词正文
            prompt_source: prompt 来源记录
            tools: 工具名称元组，None 表示不加载
            skills: skill 名称元组，None 表示不加载
            max_turns: 最大轮数，None 使用默认值
            hook_profile: Hook profile 名称，None 使用空管线
            extra_system_messages: 额外系统消息元组

        Returns:
            组装完成的 AgentExecutionProfile
        """
        agent = Agent(
            agent_id=name,
            name=name,
            description=description,
            model=self._settings.master_agent_model,  # child agent 复用 master 模型配置
            system_prompt=prompt,
            temperature=self._settings.master_agent_temperature,  # child agent 复用 master 温度
            reasoning_effort=self._settings.master_agent_reasoning_effort,  # child agent 复用 master 思考强度
        )
        loaded_skills, skill_messages = self._load_skill_messages(skills)  # 加载已存在的 skill
        registry = self._build_tool_registry(tools)  # 按名称组装工具注册表并过滤主控工具
        return AgentExecutionProfile(
            agent_id=name,
            agent=agent,
            prompt_source=prompt_source,
            runtime=self._runtime,
            tool_registry=registry,
            tool_hook_pipeline=self._resolve_tool_hook_pipeline(hook_profile),  # 解析 Hook 管线
            max_turns=max_turns or self._default_max_turns,
            skills=loaded_skills,
            extra_system_messages=tuple(extra_system_messages) + skill_messages,  # 合并预设消息和 skill 内容
        )

    def _resolve_default_prompt(self, prompt_file: str) -> Path:
        """把默认 prompt_file 解析到默认子代理包目录内。

        安全约束：不允许使用绝对路径或 app/ 前缀路径，
        防止配置文件引用到工程外部或绕过包目录解析。

        Args:
            prompt_file: prompt 文件相对路径

        Returns:
            完整的 prompt 文件路径

        Raises:
            ValueError: prompt_file 使用了绝对路径或 app/ 前缀
        """
        if Path(prompt_file).is_absolute() or prompt_file.startswith("app/"):  # 禁止工程路径和绝对路径
            raise ValueError(
                f"{ErrorCode.INVALID_SUBAGENT_CONFIG.value}: 默认子代理 prompt_file 不能使用工程路径: {prompt_file}"
            )
        return self._default_prompt_root / prompt_file  # 相对于默认子代理包目录解析

    def _build_tool_registry(self, tool_names: tuple[str, ...] | None) -> ToolRegistry:
        """按名称组装 child 工具注册表，并自动过滤主控工具。

        tools is None 时返回空 ToolRegistry（不加载任何工具）。
        主控工具（Task、ListResumableSubagents 等）自动跳过不报错，
        未知非主控工具报 INVALID_SUBAGENT_CONFIG。

        Args:
            tool_names: 工具名称元组，None 表示不加载

        Returns:
            组装完成的 ToolRegistry

        Raises:
            ValueError: 工具名称不在全局工具目录中
        """
        registry = ToolRegistry()  # 创建空注册表
        if tool_names is None:  # None 表示不加载任何工具
            return registry
        for tool_name in tool_names:  # 遍历工具名称列表
            if tool_name in CHILD_FILTERED_TOOL_NAMES:  # 主控工具自动过滤，不报错
                continue
            tool = self._tool_catalog.get(tool_name)  # 从全局目录查找工具实例
            if tool is None:  # 工具不存在时报错
                raise ValueError(f"{ErrorCode.INVALID_SUBAGENT_CONFIG.value}: 未知工具: {tool_name}")
            registry.register(tool)  # 注册工具到当前注册表
        return registry

    def _load_skill_messages(self, skill_names: tuple[str, ...] | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """加载已存在 skill 的内容并转成系统消息格式。

        None 表示不加载任何 skill。缺失的 skill 静默跳过不报错。

        Args:
            skill_names: skill 名称元组，None 表示不加载

        Returns:
            (已加载的 skill 名称元组, skill 内容消息元组)
        """
        if skill_names is None:  # None 表示不加载任何 skill
            return (), ()
        loaded_names: list[str] = []  # 成功加载的 skill 名称列表
        messages: list[str] = []  # skill 内容消息列表
        for skill_name in skill_names:  # 遍历 skill 名称
            try:
                document = self._skill_catalog.get(skill_name)  # 尝试从索引获取 skill 文档
            except ValueError:  # 缺失的 skill 静默跳过
                continue
            loaded_names.append(document.name)  # 记录成功加载的 skill 名称
            messages.append(f"<skill name=\"{document.name}\">\n{document.content}\n</skill>")  # 格式化为系统消息
        return tuple(loaded_names), tuple(messages)

    def _resolve_tool_hook_pipeline(self, hook_profile: str | None) -> ToolHookPipeline:
        """解析 Hook profile 名称到 ToolHookPipeline 实例。

        None 时使用空 ToolHookPipeline，不加载任何 Hook。

        Args:
            hook_profile: Hook profile 名称，None 表示不使用 Hook

        Returns:
            对应的 ToolHookPipeline 实例

        Raises:
            ValueError: 指定名称的 profile 在注册表中不存在
        """
        if hook_profile is None:  # None 表示不启用 Hook
            return ToolHookPipeline()
        return self._hook_profiles.get(hook_profile)  # 从注册表获取指定 profile
