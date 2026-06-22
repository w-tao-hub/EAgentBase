"""依赖注入容器（Composition Root）。

负责创建所有依赖实例并将它们组装在一起。
是整个应用的唯一"new"集中点。

v2 更新：工具注册改为先收集到 base_tool_catalog，
再按需组装到 master profile 和各 child profile 的工具注册表中。
AgentLoop 改为无状态设计，只接收 default_max_turns。
Provider 改为多 profile 注册中心。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from app.config import Settings
from app.infra.store.redis_session_store import RedisSessionStore
from app.infra.store.redis_run_store import RedisRunStore
from app.infra.store.redis_lock_store import RedisLockStore
from app.infra.store.redis_task_store import RedisTaskStore
from app.infra.store.redis_tool_result_store import RedisToolResultStore
from app.infra.store.redis_run_cancel_bus import RedisRunCancelBus
from app.infra.store.redis_store_transaction import RedisStoreTransaction
from app.infra.agents.master_agent_provider import (
    MASTER_AGENT_DEFINITIONS,
    MasterAgentProvider,
    load_master_agent,
)
from app.core.models.error import ErrorCode
from app.core.runtime.context_builder import NoTrimPolicy, TokenBudgetCompressionPolicy
from app.core.hooks import (
    ModelHookPipeline,
    NoOpStreamTextGuard,
    PersistLargeToolResultHook,
    ToolHookPipeline,
)
from app.core.models.agent import AgentExecutionProfile, AgentPromptSource
from app.core.models.tool import Tool, ToolRegistry
from app.core.loop.agent_loop import AgentLoop
from app.infra.skills.catalog import SkillCatalog
from app.infra.tools.plan_create_tool import PlanCreateTool
from app.infra.tools.plan_get_tool import PlanGetTool
from app.infra.tools.plan_list_tool import PlanListTool
from app.infra.tools.query_tool_result_tool import QueryToolResultTool
from app.infra.tools.run_python_script_tool import RunPythonScriptTool
from app.infra.tools.skill_tool import SkillTool
from app.infra.tools.plan_update_tool import PlanUpdateTool
from app.infra.agents.default_sub_agents import DEFAULT_SUB_AGENT_DEFINITIONS
from app.infra.agents.custom_sub_agent_loader import CustomSubAgentLoader
from app.infra.agents.profile_builder import SubAgentProfileBuilder
from app.infra.agents.hook_profiles import HookRegistry
from app.services.child_agent_runner import ChildAgentRunner
from app.infra.tools.task_tool import TaskTool
from app.infra.tools.list_resumable_subagents_tool import ListResumableSubagentsTool
from app.services.chat_event_processor import ChatEventProcessor
from app.services.session_service import SessionService
from app.services.task_service import TaskService
from app.services.run_control_service import RunControlService
from app.services.chat_service import ChatService
from app.services.session_cleanup_service import SessionCleanupService

# base tool 固定名称集合，用于区分固定 tool 与 MCP 动态 tool。
# 当 _MASTER_TOOL_MOUNTS 指定了挂载列表时，只过滤这些固定 tool；MCP 动态 tool 始终全部注册。
_FIXED_TOOL_NAMES: frozenset[str] = frozenset({
    "plan_create",
    "plan_get",
    "plan_update",
    "plan_list",
    "skill",
    "query_tool_result",
    "run_python_script",
})

# 按主代理名称控制 tool/hook/skill 挂载。
# name 不在字典中 → 不挂载任何 tool/hook/skill。
_MASTER_TOOL_MOUNTS: dict[str, tuple[str, ...]] = {
    "default": ("plan_create", "plan_get", "plan_update", "plan_list", "skill", "query_tool_result", "run_python_script"),
}
_MASTER_HOOK_MOUNTS: dict[str, tuple[str, ...]] = {
    "default": ("persist_large_result",),
}
_MASTER_SKILL_MOUNTS: dict[str, tuple[str, ...]] = {
    "default": ("test-skill",),
}

if TYPE_CHECKING:
    from app.infra.tools.mcp_client_manager import MCPClientManager
    from redis.asyncio import Redis


@dataclass
class _InfraBundle:
    """基础设施装配结果。"""

    redis: "Redis"
    pubsub_redis: "Redis"
    session_store: RedisSessionStore
    run_store: RedisRunStore
    lock_store: RedisLockStore
    task_store: RedisTaskStore
    tool_result_store: RedisToolResultStore
    store_transaction: RedisStoreTransaction
    run_cancel_bus: RedisRunCancelBus


@dataclass
class _RuntimeBundle:
    """运行时与 Hook 装配结果。"""

    llm_adapter: object
    runtime: object
    tool_hook_pipeline: ToolHookPipeline
    hook_registry: "HookRegistry"


@dataclass
class _ToolingBundle:
    """工具与技能装配结果。"""

    task_service: TaskService
    skill_catalog: SkillCatalog
    skill_reminder: str
    base_tool_catalog: dict[str, Tool]
    mcp_client_manager: "MCPClientManager | None"


@dataclass
class _ProfilesBundle:
    """Profile 装配结果。"""

    agent_provider: MasterAgentProvider
    agent_loop: AgentLoop
    context_trim_policy: object
    child_runner: ChildAgentRunner


@dataclass
class _ServicesBundle:
    """服务装配结果。"""

    session_service: SessionService
    run_control_service: RunControlService
    chat_service: ChatService
    session_cleanup_service: SessionCleanupService


@dataclass
class Container:
    """依赖注入容器。

    持有所有服务实例的引用，提供统一的生命周期管理。
    通过 Container.create() 工厂方法创建实例。
    """

    session_service: SessionService
    run_control_service: RunControlService
    chat_service: ChatService
    session_cleanup_service: SessionCleanupService

    _redis: Redis = field(repr=False)
    _pubsub_redis: Redis = field(repr=False)
    _owns_redis: bool = field(default=False, repr=False)
    _owns_pubsub_redis: bool = field(default=False, repr=False)
    _mcp_client_manager: "MCPClientManager | None" = field(default=None, repr=False)
    _agent_provider: "object" = field(default=None, repr=False)

    @staticmethod
    def _build_redis_client_kwargs() -> dict[str, object]:
        """构建主 Redis 客户端通用连接参数。"""
        # 这些参数同时用于单点模式和 Sentinel 模式下的 master 客户端。
        return {
            "decode_responses": True,
            "socket_keepalive": True,
            # socket_keepalive_options 仅 Linux 支持，macOS/Windows 会报错，因此这里不显式传。
            "health_check_interval": 30,
            "socket_connect_timeout": 5,
            "socket_timeout": 30,
            "retry_on_timeout": True,
            "retry_on_error": [ConnectionError, TimeoutError],
        }

    @classmethod
    def _build_single_redis_client(cls, settings: Settings, max_connections: int) -> "Redis":
        """基于单点 URL 创建 Redis 客户端。"""
        import redis.asyncio as aioredis

        client_kwargs = cls._build_redis_client_kwargs()
        if settings.redis_url is None:
            raise ValueError("REDIS_MODE=single 时必须提供 REDIS_URL")
        pool = aioredis.BlockingConnectionPool.from_url(
            settings.redis_url,
            max_connections=max_connections,
            timeout=5,
            **client_kwargs,
        )
        return aioredis.Redis(connection_pool=pool)

    @staticmethod
    def _build_sentinel_kwargs(settings: Settings) -> dict[str, object]:
        """构建 Sentinel 节点连接参数。"""
        sentinel_kwargs: dict[str, object] = {
            "decode_responses": True,
            "socket_keepalive": True,
            "socket_connect_timeout": 5,
            "socket_timeout": 30,
        }
        # 当前版本默认让 Sentinel 与 master 共用同一套认证信息。
        if settings.redis_username is not None:
            sentinel_kwargs["username"] = settings.redis_username
        if settings.redis_password is not None:
            sentinel_kwargs["password"] = settings.redis_password
        return sentinel_kwargs

    @classmethod
    def _build_sentinel_connection_kwargs(cls, settings: Settings) -> dict[str, object]:
        """构建 Sentinel 模式下 master Redis 的通用连接参数。"""
        master_kwargs = cls._build_redis_client_kwargs()
        master_kwargs["db"] = settings.redis_db
        if settings.redis_username is not None:
            master_kwargs["username"] = settings.redis_username
        if settings.redis_password is not None:
            master_kwargs["password"] = settings.redis_password
        return master_kwargs

    @staticmethod
    def _parse_sentinel_nodes(nodes: list[str]) -> list[tuple[str, int]]:
        """把 `host:port` 形式的节点列表解析成 Sentinel 所需元组。"""
        parsed_nodes: list[tuple[str, int]] = []
        for node in nodes:
            # Sentinel 环境变量当前约定为 `host:port`；这里在容器边界做显式校验。
            host, separator, raw_port = node.rpartition(":")
            if separator == "" or host.strip() == "" or raw_port.strip() == "":
                raise ValueError(f"无效的 REDIS_SENTINEL_NODES 节点格式: {node}")
            try:
                parsed_nodes.append((host.strip(), int(raw_port.strip())))
            except ValueError as exc:
                raise ValueError(f"REDIS_SENTINEL_NODES 端口必须是整数: {node}") from exc
        return parsed_nodes

    @classmethod
    def _build_sentinel_redis_client(cls, settings: Settings, max_connections: int) -> "Redis":
        """基于 Sentinel 创建指向当前 master 的 Redis 客户端。"""
        from redis.asyncio.sentinel import Sentinel

        sentinel = Sentinel(
            cls._parse_sentinel_nodes(settings.redis_sentinel_nodes),
            sentinel_kwargs=cls._build_sentinel_kwargs(settings),
            **cls._build_sentinel_connection_kwargs(settings),
        )
        # master_for 返回的仍是标准 asyncio Redis 客户端，后续 store/service 无需感知 Sentinel。
        return sentinel.master_for(
            settings.redis_sentinel_master_name,
            max_connections=max_connections,
        )

    @classmethod
    def _create_redis(cls, settings: Settings) -> "Redis":
        """创建主 Redis 客户端。"""
        # 将 Redis 连接细节收口在容器内部，避免把基础设施创建逻辑散落到应用工厂。
        # 测试若需替换 Redis，应 patch 该 helper，而不是扩张 create_app 的参数面。
        if settings.redis_mode == "sentinel":
            return cls._build_sentinel_redis_client(settings, max_connections=50)
        return cls._build_single_redis_client(settings, max_connections=50)

    @classmethod
    def _create_pubsub_redis(cls, settings: Settings) -> "Redis":
        """创建 pubsub 专用 Redis 客户端。"""
        # 该客户端只服务 ChatService 的全局取消监听器，
        # 因此连接池保持极小规模，避免和普通读写命令争抢主连接池容量。
        if settings.redis_mode == "sentinel":
            return cls._build_sentinel_redis_client(settings, max_connections=2)
        return cls._build_single_redis_client(settings, max_connections=2)

    @staticmethod
    async def _warmup_redis_pool(redis: "Redis", target_connections: int = 150) -> None:
        """预热 Redis 连接池。"""
        import asyncio
        import time as time_mod

        start = time_mod.perf_counter()
        await asyncio.gather(*[redis.ping() for _ in range(target_connections)])
        elapsed_ms = (time_mod.perf_counter() - start) * 1000
        logger = logging.getLogger(__name__)
        logger.info(
            "Redis 连接池预热完成: target=%d, elapsed_ms=%.2f",
            target_connections,
            elapsed_ms,
        )

    @staticmethod
    def _create_mcp_client_manager(settings: Settings) -> "MCPClientManager":
        """创建 MCP 客户端管理器。"""
        from app.infra.tools.mcp_client_manager import MCPClientManager

        return MCPClientManager.from_settings(settings)

    @classmethod
    def _build_infra(cls, settings: Settings) -> _InfraBundle:
        """装配基础设施依赖。"""
        redis = cls._create_redis(settings)
        pubsub_redis = cls._create_pubsub_redis(settings)
        key_prefix = settings.redis_key_prefix
        session_store = RedisSessionStore(redis, key_prefix)
        run_store = RedisRunStore(redis, key_prefix)
        store_transaction = RedisStoreTransaction(
            redis=redis,
            session_store=session_store,
            run_store=run_store,
        )
        run_cancel_bus = RedisRunCancelBus(pubsub_redis)
        return _InfraBundle(
            redis=redis,
            pubsub_redis=pubsub_redis,
            session_store=session_store,
            run_store=run_store,
            lock_store=RedisLockStore(redis, key_prefix),
            task_store=RedisTaskStore(redis, key_prefix),
            tool_result_store=RedisToolResultStore(redis, key_prefix),
            store_transaction=store_transaction,
            run_cancel_bus=run_cancel_bus,
        )

    @classmethod
    def _build_runtime_bundle(cls, settings: Settings, infra: _InfraBundle) -> _RuntimeBundle:
        """装配运行时与默认 Hook 管线。"""
        model_hook_pipeline = ModelHookPipeline([])
        persist_hook = PersistLargeToolResultHook(infra.tool_result_store)
        tool_hook_pipeline = ToolHookPipeline([persist_hook])
        hook_registry = HookRegistry(
            tool_hooks={"persist_large_result": persist_hook},
            model_hooks={},
        )
        stream_text_guard = NoOpStreamTextGuard()

        from app.infra.llm.litellm_adapter import LiteLLMAdapter

        llm_adapter = LiteLLMAdapter(timeout_seconds=settings.litellm_timeout_seconds)

        from app.core.runtime.agent_runtime import AgentRuntime

        runtime = AgentRuntime(
            llm_adapter=llm_adapter,
            model_hook_pipeline=model_hook_pipeline,
            stream_text_guard=stream_text_guard,
        )
        return _RuntimeBundle(
            llm_adapter=llm_adapter,
            runtime=runtime,
            tool_hook_pipeline=tool_hook_pipeline,
            hook_registry=hook_registry,
        )

    @classmethod
    def _build_tooling_bundle(cls, settings: Settings, infra: _InfraBundle) -> _ToolingBundle:
        """装配基础工具目录、技能索引与 MCP 工具。"""
        task_service = TaskService(infra.task_store)
        skill_catalog = SkillCatalog.discover()
        skill_reminder = skill_catalog.build_system_reminder()
        base_tool_catalog: dict[str, Tool] = {}

        def register_base_tool(tool: Tool) -> None:
            """将工具注册到 base_tool_catalog，重名时抛出 ValueError。"""
            if tool.name in base_tool_catalog:
                raise ValueError(f"工具 '{tool.name}' 已注册")
            base_tool_catalog[tool.name] = tool

        register_base_tool(PlanCreateTool(task_service))
        register_base_tool(PlanGetTool(task_service))
        register_base_tool(PlanUpdateTool(task_service))
        register_base_tool(PlanListTool(task_service))
        register_base_tool(SkillTool(skill_catalog))
        register_base_tool(QueryToolResultTool(infra.tool_result_store))
        register_base_tool(RunPythonScriptTool(workspace_root=settings.workspace_root))

        mcp_client_manager = cls._create_mcp_client_manager(settings)
        for mcp_tool in mcp_client_manager.list_tools():
            register_base_tool(mcp_tool)

        return _ToolingBundle(
            task_service=task_service,
            skill_catalog=skill_catalog,
            skill_reminder=skill_reminder,
            base_tool_catalog=base_tool_catalog,
            mcp_client_manager=mcp_client_manager,
        )

    @staticmethod
    def _resolve_mount_master_agents(
        mount_master_agents: tuple[str, ...] | None,
        known_master_names: set[str],
        child_name: str,
    ) -> tuple[str, ...]:
        """把子代理挂载配置归一为主代理名称元组。"""
        # 仅在字段缺省时回退到 default；显式空列表要视为配置错误。
        resolved = ("default",) if mount_master_agents is None else mount_master_agents
        if not resolved:
            raise ValueError(f"{ErrorCode.INVALID_MASTER_AGENT_CONFIG.value}: 子代理挂载列表为空: {child_name}")
        unknown_names = sorted(set(resolved) - known_master_names)
        if unknown_names:
            raise ValueError(
                f"{ErrorCode.INVALID_MASTER_AGENT_CONFIG.value}: "
                f"子代理 {child_name} 挂载了未知主代理: {unknown_names}"
            )
        return tuple(resolved)

    @classmethod
    def _build_profiles(
        cls,
        settings: Settings,
        infra: _InfraBundle,
        runtime_bundle: _RuntimeBundle,
        tooling: _ToolingBundle,
    ) -> _ProfilesBundle:
        """装配 agent profiles 与运行侧协作者。"""
        project_root = Path(__file__).resolve().parents[2]
        default_prompt_root = project_root / "app" / "infra" / "agents" / "default_sub_agents"
        profile_builder = SubAgentProfileBuilder(
            settings=settings,
            runtime=runtime_bundle.runtime,
            tool_catalog=tooling.base_tool_catalog,
            hook_registry=runtime_bundle.hook_registry,
            skill_catalog=tooling.skill_catalog,
            default_prompt_root=default_prompt_root,
        )

        known_master_names = {definition.name for definition in MASTER_AGENT_DEFINITIONS}
        child_mounts: dict[str, tuple[str, ...]] = {}

        child_profiles: dict[str, AgentExecutionProfile] = {}
        for definition in DEFAULT_SUB_AGENT_DEFINITIONS:
            profile = profile_builder.build_default_profile(definition)
            child_profiles[profile.agent_id] = profile
            child_mounts[profile.agent_id] = cls._resolve_mount_master_agents(
                definition.mount_master_agents,
                known_master_names,
                profile.agent_id,
            )

        custom_definitions = CustomSubAgentLoader(
            project_root / "agents",
            reserved_names={
                *(defn.name for defn in DEFAULT_SUB_AGENT_DEFINITIONS),
                *(definition.name for definition in MASTER_AGENT_DEFINITIONS),
            },
        ).load()
        for definition in custom_definitions:
            profile = profile_builder.build_custom_profile(definition)
            child_profiles[profile.agent_id] = profile
            child_mounts[profile.agent_id] = cls._resolve_mount_master_agents(
                definition.mount_master_agents,
                known_master_names,
                profile.agent_id,
            )

        agent_loop = AgentLoop(default_max_turns=settings.agent_max_turns)
        context_trim_policy = (
            TokenBudgetCompressionPolicy(
                session_store=infra.session_store,
                llm_adapter=runtime_bundle.llm_adapter,
                token_threshold=settings.context_token_threshold,
            )
            if settings.context_token_threshold > 0
            else NoTrimPolicy()
        )

        child_runner = ChildAgentRunner(
            session_store=infra.session_store,
            run_store=infra.run_store,
            store_transaction=infra.store_transaction,
            agent_loop=agent_loop,
            child_profiles=child_profiles,
            settings=settings,
            tool_catalog=tooling.base_tool_catalog,
            context_trim_policy=context_trim_policy,
        )

        master_profiles: dict[str, AgentExecutionProfile] = {}
        for master_definition in MASTER_AGENT_DEFINITIONS:
            # 为每个主代理创建仅包含其可见子代理的 TaskTool
            visible_child_profiles = {
                child_name: child_profile
                for child_name, child_profile in child_profiles.items()
                if master_definition.name in child_mounts[child_name]
            }
            task_tool = TaskTool(child_runner, child_profiles=visible_child_profiles)
            list_resumable_subagents_tool = ListResumableSubagentsTool(infra.session_store)

            # 每个主代理有独立的 ToolRegistry，按 _MASTER_TOOL_MOUNTS 控制挂载
            # _FIXED_TOOL_NAMES 中的工具按列表过滤；MCP 动态工具不受限制始终注册
            allowed_tools = _MASTER_TOOL_MOUNTS.get(master_definition.name)
            master_tool_registry = ToolRegistry()
            if allowed_tools is not None:
                for name, tool in tooling.base_tool_catalog.items():
                    if name not in _FIXED_TOOL_NAMES or name in allowed_tools:
                        master_tool_registry.register(tool)
            master_tool_registry.register(task_tool)
            master_tool_registry.register(list_resumable_subagents_tool)

            # 按 _MASTER_HOOK_MOUNTS 为当前主代理装配独立的 tool_hook_pipeline
            hook_names = _MASTER_HOOK_MOUNTS.get(master_definition.name)
            if hook_names:
                hooks = [runtime_bundle.hook_registry.get_tool_hook(n) for n in hook_names]
                tool_hook_pipeline = ToolHookPipeline(hooks)
            else:
                tool_hook_pipeline = ToolHookPipeline()

            # 按 _MASTER_SKILL_MOUNTS 为当前主代理装配 extra_system_messages
            # skill 没有全局默认，只在字典中显式配置才加入
            skill_names = _MASTER_SKILL_MOUNTS.get(master_definition.name)
            skill_contents: list[str] = []
            if skill_names:
                for n in skill_names:
                    try:
                        skill_contents.append(tooling.skill_catalog.get(n).content)
                    except ValueError:
                        pass  # skill 不存在则跳过
            extra_system_messages = tuple(skill_contents) if skill_contents else ()

            # 加载主代理
            master_agent = load_master_agent(settings=settings, definition=master_definition)
            prompt_path = project_root / "app" / "infra" / "agents" / master_definition.prompt_file
            master_profiles[master_definition.name] = AgentExecutionProfile(
                agent_id=master_agent.agent_id,
                agent=master_agent,
                prompt_source=AgentPromptSource(kind="file", path=str(prompt_path)),
                runtime=runtime_bundle.runtime,
                tool_registry=master_tool_registry,
                tool_hook_pipeline=tool_hook_pipeline,
                max_turns=settings.agent_max_turns,
                extra_system_messages=extra_system_messages,
            )

        agent_provider = MasterAgentProvider(
            master_profiles=master_profiles,
            child_profiles=child_profiles,
        )
        return _ProfilesBundle(
            agent_provider=agent_provider,
            agent_loop=agent_loop,
            context_trim_policy=context_trim_policy,
            child_runner=child_runner,
        )

    @classmethod
    def _build_services(
        cls,
        settings: Settings,
        infra: _InfraBundle,
        profiles: _ProfilesBundle,
    ) -> _ServicesBundle:
        """装配对外 service。"""
        session_service = SessionService(
            session_store=infra.session_store,
            lock_store=infra.lock_store,
            agent_provider=profiles.agent_provider,
        )
        chat_event_processor = ChatEventProcessor(
            infra.session_store,
            child_runner=profiles.child_runner,
        )
        chat_service = ChatService(
            session_store=infra.session_store,
            run_store=infra.run_store,
            lock_store=infra.lock_store,
            store_transaction=infra.store_transaction,
            run_cancel_bus=infra.run_cancel_bus,
            agent_provider=profiles.agent_provider,
            agent_loop=profiles.agent_loop,
            settings=settings,
            context_trim_policy=profiles.context_trim_policy,
            event_processor=chat_event_processor,
        )
        run_control_service = RunControlService(
            run_store=infra.run_store,
            chat_service=chat_service,
        )
        session_cleanup_service = SessionCleanupService(
            session_store=infra.session_store,
            run_store=infra.run_store,
            tool_result_store=infra.tool_result_store,
        )
        return _ServicesBundle(
            session_service=session_service,
            run_control_service=run_control_service,
            chat_service=chat_service,
            session_cleanup_service=session_cleanup_service,
        )

    @classmethod
    def create(
        cls,
        settings: Settings | None = None,
    ) -> Container:
        """创建并组装完整的依赖容器。"""
        if settings is None:
            settings = Settings(redis_url="redis://localhost:6379")

        infra = cls._build_infra(settings)
        runtime_bundle = cls._build_runtime_bundle(settings, infra)
        tooling = cls._build_tooling_bundle(settings, infra)
        profiles = cls._build_profiles(
            settings,
            infra,
            runtime_bundle,
            tooling,
        )
        services = cls._build_services(
            settings,
            infra,
            profiles,
        )

        return cls(
            session_service=services.session_service,
            run_control_service=services.run_control_service,
            chat_service=services.chat_service,
            session_cleanup_service=services.session_cleanup_service,
            _redis=infra.redis,
            _pubsub_redis=infra.pubsub_redis,
            _owns_redis=True,
            _owns_pubsub_redis=True,
            _mcp_client_manager=tooling.mcp_client_manager,
            _agent_provider=profiles.agent_provider,
        )

    async def startup(self) -> None:
        """执行容器启动期预热。"""
        await self.chat_service.start_cancel_listener()
        await self._warmup_redis_pool(self._redis, target_connections=50)

    async def close(self) -> None:
        """关闭容器，释放所有持有的资源。"""
        await self.chat_service.aclose()
        if self._mcp_client_manager is not None:
            await self._mcp_client_manager.aclose()
        if self._owns_pubsub_redis and self._pubsub_redis is not self._redis:
            await self._pubsub_redis.aclose()
        if self._owns_redis:
            await self._redis.aclose()

    async def ping_readiness(self) -> None:
        """执行基础设施就绪检查。"""
        await self._redis.ping()
