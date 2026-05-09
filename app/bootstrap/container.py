"""依赖注入容器（Composition Root）。

负责创建所有依赖实例并将它们组装在一起。
是整个应用的唯一"new"集中点。

v2 更新：工具注册改为先收集到 base_tool_catalog，
再按需组装到 master profile 和各 child profile 的工具注册表中。
AgentLoop 改为无状态设计，只接收 default_max_turns。
Provider 改为多 profile 注册中心。
"""

from __future__ import annotations  # 启用未来注解

import logging  # 导入日志，用于记录连接池预热耗时
from dataclasses import dataclass, field  # 导入数据类装饰器和字段工具
from pathlib import Path  # 导入路径处理类
from typing import TYPE_CHECKING  # 导入类型检查标记

from app.config import Settings  # 导入应用配置
from app.infra.store.redis_session_store import RedisSessionStore  # 导入 Redis 会话存储
from app.infra.store.redis_run_store import RedisRunStore  # 导入 Redis 运行存储
from app.infra.store.redis_lock_store import RedisLockStore  # 导入 Redis 锁存储
from app.infra.store.redis_task_store import RedisTaskStore  # 导入 Redis 任务存储
from app.infra.store.redis_tool_result_store import RedisToolResultStore  # 导入大工具结果存储
from app.infra.agents.master_agent_provider import MasterAgentProvider, load_master_agent  # 导入主智能体提供者和加载函数
from app.core.runtime.context_builder import NoTrimPolicy, TokenBudgetCompressionPolicy  # 导入上下文策略
from app.core.hooks import (  # 导入 Hook 相关抽象
    ModelHookPipeline,
    NoOpStreamTextGuard,
    PersistLargeToolResultHook,
    ToolHookPipeline,
)
from app.core.models.agent import AgentExecutionProfile, AgentPromptSource  # 导入执行配置和 prompt 来源
from app.core.models.tool import Tool, ToolRegistry  # 导入工具注册表和工具抽象
from app.core.loop.agent_loop import AgentLoop  # 导入 Agent 循环
from app.infra.skills.catalog import SkillCatalog  # 导入 skill 索引，负责扫描根级 skills 目录。
from app.infra.tools.plan_create_tool import PlanCreateTool  # 导入计划创建工具
from app.infra.tools.plan_get_tool import PlanGetTool  # 导入计划获取工具
from app.infra.tools.plan_list_tool import PlanListTool  # 导入计划列表工具
from app.infra.tools.query_tool_result_tool import QueryToolResultTool  # 导入大工具结果查询工具
from app.infra.tools.run_python_script_tool import RunPythonScriptTool  # 导入项目内 Python 脚本执行工具
from app.infra.tools.skill_tool import SkillTool  # 导入 skill 工具，实现 SKILL.md 全文加载。
from app.infra.tools.plan_update_tool import PlanUpdateTool  # 导入计划更新工具
from app.infra.agents.default_sub_agents import DEFAULT_SUB_AGENT_DEFINITIONS  # 导入默认子代理定义
from app.infra.agents.custom_sub_agent_loader import CustomSubAgentLoader  # 导入自定义子代理加载器
from app.infra.agents.profile_builder import SubAgentProfileBuilder  # 导入 profile 组装器
from app.infra.agents.hook_profiles import HookProfileRegistry  # 导入 Hook profile 注册表
from app.services.child_agent_runner import ChildAgentRunner  # 导入子代理执行服务
from app.infra.tools.task_tool import TaskTool  # 导入 Task 派发工具
from app.infra.tools.list_resumable_subagents_tool import ListResumableSubagentsTool  # 导入可恢复子代理列表查询工具
from app.services.chat_event_processor import ChatEventProcessor  # 导入聊天事件分发器
from app.services.session_service import SessionService  # 导入会话服务
from app.services.task_service import TaskService  # 导入任务业务服务
from app.services.run_control_service import RunControlService  # 导入运行控制服务
from app.services.chat_service import ChatService  # 导入聊天服务
from app.services.session_cleanup_service import SessionCleanupService  # 导入会话级联删除服务

if TYPE_CHECKING:  # 仅在类型检查时导入
    from app.infra.tools.mcp_client_manager import MCPClientManager  # MCP 客户端管理器类型。
    from redis.asyncio import Redis  # 异步 Redis 客户端类型


@dataclass  # 使用数据类承载基础设施装配结果，避免跨阶段传递位置敏感 tuple。
class _InfraBundle:
    """基础设施装配结果。"""

    redis: "Redis"  # 主 Redis 客户端，供普通命令、存储与广播复用。
    pubsub_redis: "Redis"  # pubsub 专用 Redis 客户端，供取消监听长连接使用。
    session_store: RedisSessionStore  # 会话存储。
    run_store: RedisRunStore  # Run 存储。
    lock_store: RedisLockStore  # 会话锁存储。
    task_store: RedisTaskStore  # Task 存储。
    tool_result_store: RedisToolResultStore  # 大工具结果存储。


@dataclass  # 使用数据类承载运行时装配结果，避免 create() 关心底层对象细节。
class _RuntimeBundle:
    """运行时与 Hook 装配结果。"""

    llm_adapter: object  # LiteLLM 适配器实例，供运行时与压缩策略共享。
    runtime: object  # AgentRuntime 实例，供 master 与 child profile 共享。
    tool_hook_pipeline: ToolHookPipeline  # 主智能体工具 Hook 管线。


@dataclass  # 使用数据类承载工具目录装配结果，便于 profile 阶段复用。
class _ToolingBundle:
    """工具与技能装配结果。"""

    task_service: TaskService  # Task 业务服务，供任务工具复用。
    skill_catalog: SkillCatalog  # Skill 索引，供 skill 工具与 profile 装配复用。
    skill_reminder: str  # 根级 skills 提醒文案，供主智能体系统消息注入。
    base_tool_catalog: dict[str, Tool]  # 全局基础工具目录，按名称索引。
    mcp_client_manager: "MCPClientManager | None"  # MCP 管理器，供容器关闭阶段统一回收。


@dataclass  # 使用数据类承载 profile 装配结果，避免 service 阶段重新感知内部装配细节。
class _ProfilesBundle:
    """Profile 装配结果。"""

    agent_provider: MasterAgentProvider  # 多 profile 注册中心。
    agent_loop: AgentLoop  # 主/子代理共享的无状态循环器。
    context_trim_policy: object  # 上下文裁剪策略，供聊天服务与子代理执行复用。
    child_runner: ChildAgentRunner  # 子代理执行服务，供 TaskTool 与事件处理器复用。


@dataclass  # 使用数据类承载服务装配结果，保持 create() 的最终返回阶段简洁。
class _ServicesBundle:
    """服务装配结果。"""

    session_service: SessionService  # 会话服务。
    run_control_service: RunControlService  # 运行控制服务。
    chat_service: ChatService  # 聊天服务。
    session_cleanup_service: SessionCleanupService  # 会话级联删除服务。


@dataclass  # 使用数据类定义容器
class Container:
    """依赖注入容器。

    持有所有服务实例的引用，提供统一的生命周期管理。
    通过 Container.create() 工厂方法创建实例。
    """

    # 会话服务
    session_service: SessionService

    # 运行控制服务
    run_control_service: RunControlService

    # 聊天服务
    chat_service: ChatService

    # 会话级联删除服务
    session_cleanup_service: SessionCleanupService

    # 内部依赖引用（不暴露给外部）
    _redis: Redis = field(repr=False)  # Redis 客户端引用，用于关闭连接
    _pubsub_redis: Redis = field(repr=False)  # pubsub 专用 Redis 客户端引用，用于关闭独立监听连接
    _owns_redis: bool = field(default=False, repr=False)  # 是否由容器自己创建的 Redis
    _owns_pubsub_redis: bool = field(default=False, repr=False)  # 是否由容器自己创建的 pubsub Redis
    _mcp_client_manager: "MCPClientManager | None" = field(default=None, repr=False)  # MCP 客户端管理器引用，用于统一生命周期管理。
    _agent_provider: "object" = field(default=None, repr=False)  # Agent 提供者引用，v2 为 MasterAgentProvider 多 profile 注册中心。

    @staticmethod
    def _create_redis(settings: Settings) -> "Redis":
        """创建 Redis 客户端。

        将 Redis 连接细节收口在容器内部，避免把基础设施创建逻辑散落到应用工厂。
        测试若需替换 Redis，应 patch 该 helper，而不是扩张 create_app 的参数面。
        """
        import redis.asyncio as aioredis  # 延迟导入异步 Redis 模块，避免模块导入时过早建立依赖

        pool = aioredis.BlockingConnectionPool.from_url(  # 使用阻塞连接池，池满时排队等待而不是直接抛异常
            settings.redis_url,  # Redis 连接地址
            max_connections=50,  # 连接池上限50个，支撑更多并发SSE与心跳续期
            timeout=5,  # 等待可用连接的最大时间（秒），避免瞬时并发直接抛 MaxConnectionsError
            decode_responses=True,  # 自动解码为字符串，必须传给连接池才能保证所有连接返回字符串
            # 连接池健康配置：防止长时间闲置连接被防火墙切断
            socket_keepalive=True,  # 启用 TCP 保活（使用系统默认参数，跨平台兼容）
            # 注意：socket_keepalive_options 仅 Linux 支持，macOS/Windows 会报错，生产环境如需精细控制可取消注释
            # socket_keepalive_options={
            #     1: 1,   # TCP_KEEPIDLE: 连接闲置1秒后发送保活探测
            #     2: 1,   # TCP_KEEPINTVL: 探测间隔1秒
            #     3: 3,   # TCP_KEEPCNT: 失败3次后断开
            # },
            health_check_interval=30,  # 每30秒检查连接健康
            # 超时配置：连接保持快速失败，读取操作给足时间防止大消息列表超时
            socket_connect_timeout=5,  # 连接超时5秒
            socket_timeout=30,  # 读取操作超时30秒，避免大列表读取超时
            # 重试配置：网络抖动时自动重试
            retry_on_timeout=True,  # 超时后自动重试
            retry_on_error=[ConnectionError, TimeoutError],  # 这些错误时重试
        )
        return aioredis.Redis(connection_pool=pool)  # 基于阻塞连接池创建 Redis 客户端

    @staticmethod
    def _create_pubsub_redis(settings: Settings) -> "Redis":
        """创建 pubsub 专用 Redis 客户端。

        该客户端只服务 ChatService 的全局取消监听器，
        因此连接池保持极小规模，避免和普通读写命令争抢主连接池容量。
        """
        import redis.asyncio as aioredis  # 延迟导入异步 Redis 模块，避免模块导入时过早建立依赖

        pool = aioredis.BlockingConnectionPool.from_url(  # 为 pubsub 单独创建连接池，物理隔离监听长连接
            settings.redis_url,
            max_connections=2,  # 预留极小连接池即可：1 条模式订阅连接 + 关闭/重连时的短暂冗余
            timeout=5,  # 保持与主池一致的阻塞等待语义，避免瞬时波动直接报连接错误
            decode_responses=True,  # 与主 Redis 客户端保持一致，统一字符串语义
            socket_keepalive=True,  # 启用 TCP 保活，降低长连接被中间网络静默掐断的概率
            health_check_interval=30,  # 定期做健康检查，提升长连稳定性
            socket_connect_timeout=5,  # 连接超时保持快速失败
            socket_timeout=30,  # 读取超时与主池保持一致，避免极端情况下无限挂起
            retry_on_timeout=True,  # 网络抖动时允许自动重试
            retry_on_error=[ConnectionError, TimeoutError],  # 与主池保持一致的瞬时错误重试策略
        )
        return aioredis.Redis(connection_pool=pool)  # 返回专用 Redis 客户端，供 ChatService 的 pubsub 监听使用

    @staticmethod
    async def _warmup_redis_pool(redis: "Redis", target_connections: int = 150) -> None:
        """预热 Redis 连接池。

        并发发送 N 个 PING，让 BlockingConnectionPool 提前建立足够的 TCP 连接，
        避免请求高峰期出现连接排队导致的 TTFB 劣化。
        """
        import asyncio  # 延迟导入，保持局部性
        import time as time_mod  # 延迟导入，避免与数据类 field 冲突

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
        """创建 MCP 客户端管理器。

        当前由容器统一托管 MCP 连接生命周期，
        这样工具注册与资源关闭都能收敛在组合根中。
        """
        from app.infra.tools.mcp_client_manager import MCPClientManager  # 延迟导入管理器，避免无配置场景过早触发依赖加载。

        return MCPClientManager.from_settings(settings)  # 基于配置创建并启动 MCP 客户端管理器。

    @classmethod
    def _build_infra(cls, settings: Settings) -> _InfraBundle:
        """装配基础设施依赖。

        该阶段只负责 Redis 客户端与各类 store 的创建，
        不触碰 runtime、tool 或 service 装配逻辑。
        """
        redis = cls._create_redis(settings)  # 在容器内部创建主 Redis 客户端，统一基础设施装配边界
        pubsub_redis = cls._create_pubsub_redis(settings)  # 创建 pubsub 专用 Redis 客户端，隔离取消监听长连接
        key_prefix = settings.redis_key_prefix  # 从配置读取 Redis key 前缀，供所有 store 共享
        return _InfraBundle(
            redis=redis,  # 保存主 Redis 客户端，供后续所有阶段复用。
            pubsub_redis=pubsub_redis,  # 保存 pubsub 专用 Redis 客户端，供聊天服务使用。
            session_store=RedisSessionStore(redis, key_prefix),  # 创建会话存储。
            run_store=RedisRunStore(redis, key_prefix),  # 创建运行存储。
            lock_store=RedisLockStore(redis, key_prefix),  # 创建锁存储。
            task_store=RedisTaskStore(redis, key_prefix),  # 创建任务存储。
            tool_result_store=RedisToolResultStore(redis, key_prefix),  # 创建大工具结果存储。
        )

    @classmethod
    def _build_runtime_bundle(cls, settings: Settings, infra: _InfraBundle) -> _RuntimeBundle:
        """装配运行时与默认 Hook 管线。

        该阶段只负责把模型调用运行时补齐，
        不关心工具目录、profile 或 service。
        """
        # 构造全局 Hook 管线与流式文本守卫。
        # model_hooks 默认为空链，不挂载任何模型 Hook。
        # tool_hooks 默认仅挂载大结果持久化 Hook，可按需在 PersistLargeToolResultHook 之后追加。
        installed_model_hooks = []  # 保持默认空模型 Hook 链，避免当前版本引入额外副作用。
        model_hook_pipeline = ModelHookPipeline(installed_model_hooks)  # 创建空模型 Hook 管线。
        installed_tool_hooks = [PersistLargeToolResultHook(infra.tool_result_store)]  # 默认只安装大结果持久化 Hook。
        tool_hook_pipeline = ToolHookPipeline(installed_tool_hooks)  # 创建工具 Hook 管线。
        stream_text_guard = NoOpStreamTextGuard()  # 创建默认 no-op 守卫。

        # 由容器统一创建 LLM 适配器与完整的 AgentRuntime，
        # 避免运行时先被创建成半成品，再通过 setter 事后补齐依赖。
        from app.infra.llm.litellm_adapter import LiteLLMAdapter  # 延迟导入真实 LLM 适配器。

        llm_adapter = LiteLLMAdapter(timeout_seconds=settings.litellm_timeout_seconds)  # 创建真实 LiteLLM 适配器。

        from app.core.runtime.agent_runtime import AgentRuntime  # 延迟导入运行时，避免不必要耦合。

        runtime = AgentRuntime(  # 创建装配完整的运行时实例。
            llm_adapter=llm_adapter,  # 注入 LLM 适配器。
            model_hook_pipeline=model_hook_pipeline,  # 注入模型 Hook 管线。
            stream_text_guard=stream_text_guard,  # 注入流式文本守卫。
        )
        return _RuntimeBundle(
            llm_adapter=llm_adapter,  # 返回适配器，供上下文压缩策略复用。
            runtime=runtime,  # 返回运行时，供所有 profile 共享。
            tool_hook_pipeline=tool_hook_pipeline,  # 返回主智能体工具 Hook 管线。
        )

    @classmethod
    def _build_tooling_bundle(cls, settings: Settings, infra: _InfraBundle) -> _ToolingBundle:
        """装配基础工具目录、技能索引与 MCP 工具。

        该阶段只关注“有哪些基础工具可用”，
        不负责具体 profile 或 service 的绑定。
        """
        task_service = TaskService(infra.task_store)  # 装配任务存储到业务服务。
        skill_catalog = SkillCatalog.discover()  # 启动时一次性扫描根级 skills 目录，建立 skill 索引。
        skill_reminder = skill_catalog.build_system_reminder()  # 预先构造 system 提醒，供后续聊天上下文注入。
        base_tool_catalog: dict[str, Tool] = {}  # 全局工具目录，按名称索引。

        def register_base_tool(tool: Tool) -> None:
            """将工具注册到 base_tool_catalog，重名时抛出 ValueError。"""
            if tool.name in base_tool_catalog:  # 检查是否已存在同名工具，防止静默覆盖。
                raise ValueError(f"工具 '{tool.name}' 已注册")
            base_tool_catalog[tool.name] = tool  # 保存工具实例，供 master 与 child profile 共享。

        register_base_tool(PlanCreateTool(task_service))  # 注册计划创建工具。
        register_base_tool(PlanGetTool(task_service))  # 注册计划获取工具。
        register_base_tool(PlanUpdateTool(task_service))  # 注册计划更新工具。
        register_base_tool(PlanListTool(task_service))  # 注册计划列表工具。
        register_base_tool(SkillTool(skill_catalog))  # 注册 skill 工具。
        register_base_tool(QueryToolResultTool(infra.tool_result_store))  # 注册大工具结果查询工具。
        register_base_tool(RunPythonScriptTool(workspace_root=settings.workspace_root))  # 注册项目内 Python 脚本执行工具。

        mcp_client_manager = cls._create_mcp_client_manager(settings)  # 创建 MCP 客户端管理器，收口远端工具装配。
        for mcp_tool in mcp_client_manager.list_tools():  # 遍历所有已发现的 MCP 工具。
            register_base_tool(mcp_tool)  # 将 MCP 工具统一注册到全局工具目录。

        return _ToolingBundle(
            task_service=task_service,  # 返回任务服务，便于后续扩展或测试观察。
            skill_catalog=skill_catalog,  # 返回 skill 索引，供 profile 装配复用。
            skill_reminder=skill_reminder,  # 返回主智能体 skill 提醒文本。
            base_tool_catalog=base_tool_catalog,  # 返回基础工具目录，供所有 profile 按需注册。
            mcp_client_manager=mcp_client_manager,  # 返回 MCP 管理器，供容器关闭阶段回收。
        )

    @classmethod
    def _build_profiles(
        cls,
        settings: Settings,
        infra: _InfraBundle,
        runtime_bundle: _RuntimeBundle,
        tooling: _ToolingBundle,
    ) -> _ProfilesBundle:
        """装配 agent profiles 与运行侧协作者。

        该阶段只负责 agent/profile 相关对象，
        不直接创建对外 service。
        """
        hook_profiles = HookProfileRegistry({})  # 当前无预注册 Hook profile，后续可在此扩展。
        project_root = Path(__file__).resolve().parents[2]  # 定位项目根目录（container.py 在 app/bootstrap/ 下）。
        default_prompt_root = project_root / "app" / "infra" / "agents" / "default_sub_agents"  # 默认子代理 prompt 文件根目录。
        profile_builder = SubAgentProfileBuilder(
            settings=settings,  # 注入应用配置。
            runtime=runtime_bundle.runtime,  # 注入运行时实例（所有 profile 共享同一 runtime）。
            tool_catalog=tooling.base_tool_catalog,  # 注入全局工具目录。
            hook_profiles=hook_profiles,  # 注入 Hook profile 注册表。
            skill_catalog=tooling.skill_catalog,  # 注入 skill 索引。
            default_prompt_root=default_prompt_root,  # 默认子代理 prompt 根目录。
        )

        child_profiles: dict[str, AgentExecutionProfile] = {}  # 子代理类型名称 -> 执行 profile 映射。
        for definition in DEFAULT_SUB_AGENT_DEFINITIONS:  # 从 Python 声明式定义构建默认子代理 profile。
            profile = profile_builder.build_default_profile(definition)  # 组装 Plan 等默认子代理 profile。
            child_profiles[profile.agent_id] = profile  # 按 agent_id 索引。
        custom_definitions = CustomSubAgentLoader(
            project_root / "agents",
            reserved_names={defn.name for defn in DEFAULT_SUB_AGENT_DEFINITIONS},
        ).load()  # 从自定义 md 文件加载自定义子代理 profile 定义。
        for definition in custom_definitions:  # 组装自定义子代理 profile。
            profile = profile_builder.build_custom_profile(definition)  # 组装自定义子代理 profile。
            child_profiles[profile.agent_id] = profile  # 按 agent_id 索引。

        agent_loop = AgentLoop(default_max_turns=settings.agent_max_turns)  # 创建无状态循环器。
        context_trim_policy = (  # 根据配置决定是否启用 token 阀值压缩策略。
            TokenBudgetCompressionPolicy(
                session_store=infra.session_store,  # 注入会话存储，供策略读写摘要边界。
                llm_adapter=runtime_bundle.llm_adapter,  # 注入 LiteLLM 适配器，供策略统计 token 与生成摘要。
                token_threshold=settings.context_token_threshold,  # 注入输入 token 阀值。
            )
            if settings.context_token_threshold > 0
            else NoTrimPolicy()
        )

        child_runner = ChildAgentRunner(
            session_store=infra.session_store,
            run_store=infra.run_store,
            redis=infra.redis,
            agent_loop=agent_loop,
            child_profiles=child_profiles,
            settings=settings,
            tool_catalog=tooling.base_tool_catalog,
            context_trim_policy=context_trim_policy,
        )  # 实例化子代理执行服务。
        task_tool = TaskTool(child_runner, child_profiles=child_profiles)  # 实例化 Task 派发工具。
        list_resumable_subagents_tool = ListResumableSubagentsTool(infra.session_store)  # 实例化可恢复子代理列表查询工具。

        master_tool_registry = ToolRegistry()  # 创建主 Agent 工具注册表。
        for tool in tooling.base_tool_catalog.values():  # 遍历全局工具目录中的所有工具。
            master_tool_registry.register(tool)  # 注册到主 Agent 工具注册表。
        master_tool_registry.register(task_tool)  # 注册 Task 工具，使主 Agent 可派发子代理。
        master_tool_registry.register(list_resumable_subagents_tool)  # 注册可恢复子代理列表查询工具。

        master_agent = load_master_agent(settings)  # 从 Settings 与 master_prompt.md 加载主 Agent 元信息。
        master_profile = AgentExecutionProfile(
            agent_id=master_agent.agent_id,  # 主 Agent ID。
            agent=master_agent,  # 主 Agent 静态配置。
            prompt_source=AgentPromptSource(
                kind="file",
                path=str(project_root / "app" / "infra" / "agents" / "master_prompt.md"),
            ),  # 记录 prompt 来源为文件。
            runtime=runtime_bundle.runtime,  # 注入运行时实例。
            tool_registry=master_tool_registry,  # 注入主 Agent 工具注册表。
            tool_hook_pipeline=runtime_bundle.tool_hook_pipeline,  # 注入工具 Hook 管线。
            max_turns=settings.agent_max_turns,  # 最大轮数。
            extra_system_messages=tuple([tooling.skill_reminder] if tooling.skill_reminder else []),  # 附加 skill 提醒。
        )

        agent_provider = MasterAgentProvider(
            default_profile=master_profile,  # 注入默认主 Agent 执行 profile。
            child_profiles=child_profiles,  # 注入子代理 profile 集合。
        )
        return _ProfilesBundle(
            agent_provider=agent_provider,  # 返回 agent provider，供 service 与容器内部引用复用。
            agent_loop=agent_loop,  # 返回共享循环器，供聊天服务复用。
            context_trim_policy=context_trim_policy,  # 返回上下文裁剪策略，供聊天服务复用。
            child_runner=child_runner,  # 返回子代理执行服务，供事件处理器复用。
        )

    @classmethod
    def _build_services(
        cls,
        settings: Settings,
        infra: _InfraBundle,
        profiles: _ProfilesBundle,
    ) -> _ServicesBundle:
        """装配对外 service。

        该阶段只把前面已经装好的 infra/profile 能力绑定到服务层，
        保持 service 构造边界独立清晰。
        """
        session_service = SessionService(
            session_store=infra.session_store,  # 注入会话存储。
            lock_store=infra.lock_store,  # 注入锁存储。
            agent_provider=profiles.agent_provider,  # 注入 Agent 提供者。
        )  # 创建会话服务。
        chat_event_processor = ChatEventProcessor(
            infra.session_store,
            child_runner=profiles.child_runner,
        )  # 创建聊天事件分发器。
        chat_service = ChatService(
            session_store=infra.session_store,  # 注入会话存储。
            run_store=infra.run_store,  # 注入运行存储。
            lock_store=infra.lock_store,  # 注入锁存储。
            agent_provider=profiles.agent_provider,  # 注入 Agent 提供者。
            agent_loop=profiles.agent_loop,  # 注入 Agent 循环。
            settings=settings,  # 注入应用配置，用于读取 session_lock_ttl_seconds。
            redis=infra.redis,  # 注入主 Redis 客户端，用于普通命令与跨 worker 取消广播。
            pubsub_redis=infra.pubsub_redis,  # 注入 pubsub 专用 Redis 客户端，用于全局取消监听。
            context_trim_policy=profiles.context_trim_policy,  # 根据配置显式注入上下文策略。
            event_processor=chat_event_processor,  # 注入带 child_runner 的事件分发器，供 Task 结果回填标记。
        )  # 创建聊天服务。
        run_control_service = RunControlService(
            run_store=infra.run_store,  # 注入运行存储。
            chat_service=chat_service,  # 注入聊天服务，用于代理取消请求。
        )  # 创建运行控制服务。
        session_cleanup_service = SessionCleanupService(
            session_store=infra.session_store,
            run_store=infra.run_store,
            tool_result_store=infra.tool_result_store,
        )  # 创建会话级联删除服务，统一编排跨 store 清理。
        return _ServicesBundle(
            session_service=session_service,  # 返回会话服务。
            run_control_service=run_control_service,  # 返回运行控制服务。
            chat_service=chat_service,  # 返回聊天服务。
            session_cleanup_service=session_cleanup_service,  # 返回会话级联删除服务。
        )

    @classmethod
    def create(  # 工厂方法
        cls,
        settings: Settings | None = None,  # 应用配置（可选）
    ) -> Container:
        """创建并组装完整的依赖容器。

        Args:
            settings: 应用配置，如果为 None 则使用默认值构造

        Returns:
            组装完成的 Container 实例
        """
        # 如果没有提供 Settings，使用默认值构造
        if settings is None:  # 未提供配置
            settings = Settings(redis_url="redis://localhost:6379")  # 使用默认配置

        infra = cls._build_infra(settings)  # 先装配基础设施依赖，收口 Redis 与 store 创建。
        runtime_bundle = cls._build_runtime_bundle(settings, infra)  # 再装配运行时与默认 Hook 管线。
        tooling = cls._build_tooling_bundle(settings, infra)  # 装配基础工具目录、技能索引与 MCP 工具。
        profiles = cls._build_profiles(
            settings,
            infra,
            runtime_bundle,
            tooling,
        )  # 先装配 agent/profile 相关对象。
        services = cls._build_services(
            settings,
            infra,
            profiles,
        )  # 再把 infra 与 profiles 绑定成最终对外服务。

        # 构造容器实例
        return cls(
            session_service=services.session_service,  # 会话服务
            run_control_service=services.run_control_service,  # 运行控制服务
            chat_service=services.chat_service,  # 聊天服务
            session_cleanup_service=services.session_cleanup_service,  # 会话级联删除服务
            _redis=infra.redis,  # 主 Redis 客户端
            _pubsub_redis=infra.pubsub_redis,  # pubsub 专用 Redis 客户端
            _owns_redis=True,  # Redis 由容器创建，应由容器负责管理生命周期
            _owns_pubsub_redis=True,  # pubsub Redis 由容器创建，应由容器负责管理生命周期
            _mcp_client_manager=tooling.mcp_client_manager,  # 保存 MCP 客户端管理器引用，供关闭阶段统一释放资源。
            _agent_provider=profiles.agent_provider,  # 保存 Agent 提供者引用，供测试和内部使用。
        )

    async def startup(self) -> None:
        """执行容器启动期预热。

        当前包含两件事：
        1. 启动 ChatService 的全局取消监听器，避免首个 SSE 请求承担 pubsub 初始化成本
        2. 预热主 Redis 连接池，降低请求高峰初期的连接排队时间
        """
        await self.chat_service.start_cancel_listener()  # 提前拉起全局取消监听器，让 pubsub 长连接在启动期就绪
        await self._warmup_redis_pool(self._redis, target_connections=50)  # 主连接池继续沿用既有预热策略

    async def close(self) -> None:
        """关闭容器，释放所有持有的资源。

        当应用关闭时调用，确保 Redis 连接等资源被正确释放。
        仅在容器自己创建了 Redis 连接时才关闭。
        """
        await self.chat_service.aclose()  # 先停止聊天服务后台监听器，避免 Redis 客户端关闭时仍有监听任务悬挂
        if self._mcp_client_manager is not None:  # 如果容器持有 MCP 客户端管理器。
            await self._mcp_client_manager.aclose()  # 优先关闭 MCP 连接，避免后台线程继续占用资源。
        if self._owns_pubsub_redis and self._pubsub_redis is not self._redis:  # 若 pubsub Redis 独立于主 Redis，则单独关闭它
            await self._pubsub_redis.aclose()  # 关闭 pubsub 专用 Redis 客户端，释放监听专用连接池
        if self._owns_redis:  # 如果 Redis 由容器自己创建
            await self._redis.aclose()  # 关闭主 Redis 客户端

    async def ping_readiness(self) -> None:
        """执行基础设施就绪检查。

        当前版本的 readiness 只检查 Redis 连通性。
        之所以收口为容器显式方法，
        是为了避免 HTTP Route 直接读取 `_redis` 这种内部生命周期资源。
        """
        await self._redis.ping()  # 使用容器内部持有的 Redis 客户端执行 PING
