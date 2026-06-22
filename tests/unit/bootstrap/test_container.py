"""Container Hook 装配测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.infra.llm.litellm_adapter as litellm_adapter_module
from app.bootstrap.container import Container
from app.config import Settings
from app.core.models.execution_context import ExecutionContext
from app.core.hooks import (
    NoOpStreamTextGuard,
    PersistLargeToolResultHook,
)
from app.core.models.tool import Tool, ToolResult
from app.core.runtime.agent_runtime import AgentRuntime


class StubLLMAdapter:
    """最小 LLM 适配器替身。

    该替身不承载真实推理行为，只用于验证容器会把它注入到
    自己创建的 AgentRuntime 中，而不是接收外部 runtime 实例。
    """

    async def stream_completion(self, *args, **kwargs):
        """提供最小异步生成器接口，满足 AgentRuntime 依赖。"""
        if False:  # pragma: no cover - 仅用于把函数标记为异步生成器
            yield None


class StubMCPTool(Tool):
    """模拟 MCP 工具。"""

    def __init__(self, tool_name: str) -> None:
        self._tool_name = tool_name

    @property
    def name(self) -> str:
        return self._tool_name

    @property
    def description(self) -> str:
        return "stub mcp tool"

    @property
    def input_schema(self) -> dict:
        return {"type": "object"}

    async def call(self, input: dict, context: ExecutionContext) -> ToolResult:
        return ToolResult(content="ok", is_error=False)


class StubMCPClientManager:
    """模拟容器内的 MCP 管理器。"""

    def __init__(self, tools: list[Tool]) -> None:
        self._tools = tools
        self.closed = False

    def list_tools(self) -> list[Tool]:
        return list(self._tools)

    async def aclose(self) -> None:
        self.closed = True


def _build_tool_context(session_id: str = "task-smoke") -> ExecutionContext:
    """构造任务工具冒烟测试所需的执行上下文。"""
    from app.core.models.agent import Agent

    agent = Agent(  # 构造最小可用的 Agent 配置。
        agent_id="agent-smoke",  # 设置固定的测试 Agent ID。
        name="Task Smoke Agent",  # 设置测试 Agent 名称。
        model="gpt-4",  # 设置一个占位模型名，满足上下文结构要求。
        system_prompt="test",  # 设置最小系统提示词。
        temperature=0.0,  # 设置确定性温度，避免无关差异。
    )
    return ExecutionContext(  # 返回完整执行上下文。
        run_id="run-smoke",  # 设置固定 run_id，便于测试追踪。
        session_id=session_id,  # 使用传入会话 ID 绑定任务数据隔离。
        metadata={},  # 提供空元数据，满足上下文字段要求。
        agent=agent,  # 注入刚构造的 Agent。
    )


def _patch_container_redis(monkeypatch, redis, pubsub_redis=None) -> None:
    """统一替换容器内主 Redis 与 pubsub Redis 的创建点。"""
    selected_pubsub_redis = pubsub_redis or redis  # 若测试未显式提供第二个替身，则默认复用主 Redis，简化替身装配
    monkeypatch.setattr(Container, "_create_redis", staticmethod(lambda settings: redis))  # 拦截主 Redis 创建，改为返回测试替身
    monkeypatch.setattr(  # 拦截 pubsub Redis 创建，避免容器在测试里意外建立真实连接
        Container,
        "_create_pubsub_redis",
        staticmethod(lambda settings: selected_pubsub_redis),
    )


def test_create_redis_and_pubsub_redis_use_single_url_pools(monkeypatch) -> None:
    """验证单点模式下主 Redis 与 pubsub Redis 都通过 URL 连接池创建。"""
    import redis.asyncio as aioredis

    pool_calls: list[tuple[str, dict, object]] = []
    created_pools = [object(), object()]

    class StubRedisClient:
        """记录连接池注入结果的最小 Redis 客户端替身。"""

        def __init__(self, connection_pool) -> None:
            self.connection_pool = connection_pool

    def fake_from_url(url: str, **kwargs):
        """替换 from_url，记录每次连接池创建参数。"""
        created_pool = created_pools[len(pool_calls)]
        pool_calls.append((url, kwargs, created_pool))
        return created_pool

    monkeypatch.setattr(aioredis.BlockingConnectionPool, "from_url", staticmethod(fake_from_url))
    monkeypatch.setattr(aioredis, "Redis", StubRedisClient)

    settings = Settings(redis_url="redis://localhost:6379/9")

    redis_client = Container._create_redis(settings)
    pubsub_redis_client = Container._create_pubsub_redis(settings)

    assert pool_calls[0][0] == "redis://localhost:6379/9"
    assert pool_calls[0][1]["max_connections"] == 50
    assert pool_calls[0][1]["timeout"] == 5
    assert pool_calls[0][1]["decode_responses"] is True
    assert pool_calls[0][1]["socket_keepalive"] is True
    assert pool_calls[0][1]["health_check_interval"] == 30
    assert pool_calls[0][1]["socket_connect_timeout"] == 5
    assert pool_calls[0][1]["socket_timeout"] == 30
    assert pool_calls[0][1]["retry_on_timeout"] is True
    assert pool_calls[0][1]["retry_on_error"] == [ConnectionError, TimeoutError]

    assert pool_calls[1][0] == "redis://localhost:6379/9"
    assert pool_calls[1][1]["max_connections"] == 2
    assert pool_calls[1][1]["timeout"] == 5

    assert redis_client.connection_pool is created_pools[0]
    assert pubsub_redis_client.connection_pool is created_pools[1]


def test_create_redis_and_pubsub_redis_use_sentinel_master_discovery(monkeypatch) -> None:
    """验证 Sentinel 模式下主 Redis 与 pubsub Redis 都通过 master discovery 创建。"""
    import redis.asyncio.sentinel as sentinel_module

    sentinel_init_calls: list[dict[str, object]] = []
    master_for_calls: list[dict[str, object]] = []
    created_clients = [object(), object()]

    class StubSentinel:
        """记录 Sentinel 构造与 master_for 调用参数。"""

        def __init__(self, sentinels, sentinel_kwargs=None, **kwargs) -> None:
            sentinel_init_calls.append(
                {
                    "sentinels": sentinels,
                    "sentinel_kwargs": sentinel_kwargs,
                    "kwargs": kwargs,
                }
            )

        def master_for(self, service_name: str, **kwargs):
            master_for_calls.append(
                {
                    "service_name": service_name,
                    "kwargs": kwargs,
                }
            )
            return created_clients[len(master_for_calls) - 1]

    monkeypatch.setattr(sentinel_module, "Sentinel", StubSentinel)

    settings = Settings(
        redis_mode="sentinel",
        redis_sentinel_nodes="10.0.0.1:26379,10.0.0.2:26379",
        redis_sentinel_master_name="mymaster",
        redis_db=5,
        redis_username="sentinel-user",
        redis_password="sentinel-pass",
    )

    redis_client = Container._create_redis(settings)
    pubsub_redis_client = Container._create_pubsub_redis(settings)

    assert sentinel_init_calls[0]["sentinels"] == [("10.0.0.1", 26379), ("10.0.0.2", 26379)]
    assert sentinel_init_calls[0]["sentinel_kwargs"] == {
        "decode_responses": True,
        "socket_keepalive": True,
        "socket_connect_timeout": 5,
        "socket_timeout": 30,
        "username": "sentinel-user",
        "password": "sentinel-pass",
    }
    assert sentinel_init_calls[0]["kwargs"] == {
        "decode_responses": True,
        "socket_keepalive": True,
        "health_check_interval": 30,
        "socket_connect_timeout": 5,
        "socket_timeout": 30,
        "retry_on_timeout": True,
        "retry_on_error": [ConnectionError, TimeoutError],
        "db": 5,
        "username": "sentinel-user",
        "password": "sentinel-pass",
    }

    assert sentinel_init_calls[1] == sentinel_init_calls[0]
    assert master_for_calls == [
        {
            "service_name": "mymaster",
            "kwargs": {"max_connections": 50},
        },
        {
            "service_name": "mymaster",
            "kwargs": {"max_connections": 2},
        },
    ]

    assert redis_client is created_clients[0]
    assert pubsub_redis_client is created_clients[1]


def test_create_redis_rejects_invalid_sentinel_node_format() -> None:
    """验证 Sentinel 节点格式非法时会在客户端创建阶段失败。"""
    settings = Settings(
        redis_mode="sentinel",
        redis_sentinel_nodes=["10.0.0.1"],
        redis_sentinel_master_name="mymaster",
    )

    with pytest.raises(ValueError, match="无效的 REDIS_SENTINEL_NODES 节点格式"):
        Container._create_redis(settings)


@pytest.mark.asyncio
async def test_container_create_keeps_hook_pipeline_empty_by_default(fake_redis, monkeypatch) -> None:
    """验证未显式传入 Hook 时，容器仍会自动装配大结果持久化 Hook。"""
    llm_adapter = StubLLMAdapter()  # 创建最小 LLM 适配器替身
    monkeypatch.setattr(  # 拦截容器内部真实适配器创建，改为返回测试替身
        litellm_adapter_module,
        "LiteLLMAdapter",
        lambda *args, **kwargs: llm_adapter,
    )
    _patch_container_redis(monkeypatch, fake_redis)  # 统一拦截主 Redis 与 pubsub Redis 创建，避免测试触碰真实 Redis
    monkeypatch.setattr(  # 拦截 MCP 管理器创建，避免本地 `.env` 配置把该测试带去连接真实服务。
        Container,
        "_create_mcp_client_manager",
        staticmethod(lambda settings: StubMCPClientManager([])),
    )

    container = Container.create(  # 创建容器，验证未显式传 Hook 时的装配行为
        settings=Settings(redis_url="redis://localhost:6379"),
    )

    runtime = container._agent_provider.get_default_profile().runtime  # 读取容器内部创建的真实运行时
    model_hooks = runtime._model_hook_pipeline.hooks  # 读取模型 Hook 链
    tool_hooks = container._agent_provider.get_default_profile().tool_hook_pipeline.hooks  # 读取工具 Hook 链

    assert isinstance(runtime, AgentRuntime)  # 验证容器内部确实创建了真实 AgentRuntime
    assert runtime._llm_adapter is llm_adapter  # 验证运行时持有的是传入的更窄适配器依赖
    assert model_hooks == []  # 验证默认不挂载模型 Hook
    assert len(tool_hooks) == 1  # 默认应只自动挂载一个大结果持久化 Hook。
    assert isinstance(tool_hooks[0], PersistLargeToolResultHook)  # 验证默认自动装配的是大结果持久化 Hook。
    assert isinstance(runtime._stream_text_guard, NoOpStreamTextGuard)  # 验证默认文本守卫为 no-op

    # 验证新端口已注入 ChatService
    assert container.chat_service._store_transaction is not None
    assert container.chat_service._run_cancel_bus is not None


@pytest.mark.asyncio
async def test_container_create_builds_default_model_and_tool_hooks(fake_redis, monkeypatch) -> None:
    """验证容器内部默认组装空模型 Hook + 仅大结果持久化的工具 Hook 链。"""
    llm_adapter = StubLLMAdapter()  # 创建最小 LLM 适配器替身
    monkeypatch.setattr(  # 拦截容器内部真实适配器创建，改为返回测试替身
        litellm_adapter_module,
        "LiteLLMAdapter",
        lambda *args, **kwargs: llm_adapter,
    )
    _patch_container_redis(monkeypatch, fake_redis)  # 统一拦截主 Redis 与 pubsub Redis 创建，避免测试触碰真实 Redis
    monkeypatch.setattr(  # 拦截 MCP 管理器创建，避免测试干扰。
        Container,
        "_create_mcp_client_manager",
        staticmethod(lambda settings: StubMCPClientManager([])),
    )

    container = Container.create(  # 创建容器，只传 settings，不传任何 Hook
        settings=Settings(redis_url="redis://localhost:6379"),
    )

    runtime = container._agent_provider.get_default_profile().runtime  # 读取容器内部创建的真实运行时
    installed_tool_hooks = container._agent_provider.get_default_profile().tool_hook_pipeline.hooks  # 读取最终工具 Hook 链。

    assert runtime._model_hook_pipeline.hooks == []  # 默认空模型 Hook
    assert len(installed_tool_hooks) == 1  # 默认只应有一个大结果持久化 Hook
    assert isinstance(installed_tool_hooks[0], PersistLargeToolResultHook)  # 默认唯一工具 Hook 是大结果持久化


@pytest.mark.asyncio
async def test_container_ping_readiness_delegates_to_internal_redis(fake_redis, monkeypatch) -> None:
    """验证容器通过显式 readiness 方法执行内部 Redis 探测。"""
    llm_adapter = StubLLMAdapter()  # 创建最小 LLM 适配器替身
    ping_called = False  # 记录 readiness 是否真正触达了 Redis ping
    original_ping = fake_redis.ping  # 保存原始 ping，便于在替身中继续复用

    async def fake_ping() -> bool:
        """包一层测试替身，记录调用后继续走 fakeredis 正常逻辑。"""
        nonlocal ping_called  # 允许修改外层标记
        ping_called = True  # 标记容器 readiness 已触达底层 ping
        return await original_ping()  # 继续执行原始 ping，保持行为与真实 Redis 一致

    monkeypatch.setattr(  # 拦截容器内部真实适配器创建，改为返回测试替身
        litellm_adapter_module,
        "LiteLLMAdapter",
        lambda *args, **kwargs: llm_adapter,
    )
    _patch_container_redis(monkeypatch, fake_redis)  # 统一拦截主 Redis 与 pubsub Redis 创建，避免测试触碰真实 Redis
    monkeypatch.setattr(  # 拦截 MCP 管理器创建，确保 readiness 测试只验证 Redis 探测链路。
        Container,
        "_create_mcp_client_manager",
        staticmethod(lambda settings: StubMCPClientManager([])),
    )
    monkeypatch.setattr(fake_redis, "ping", fake_ping)  # 拦截 Redis ping，记录容器是否通过显式 readiness 调用

    container = Container.create(  # 创建容器，验证显式 readiness 入口
        settings=Settings(redis_url="redis://localhost:6379"),
    )

    await container.ping_readiness()  # 调用容器 readiness 方法

    assert ping_called is True  # 验证 readiness 已通过容器方法触达内部 Redis ping


@pytest.mark.asyncio
async def test_container_startup_starts_cancel_listener_and_warms_main_pool(fake_redis, monkeypatch) -> None:
    """验证容器启动期会预热全局取消监听器与主 Redis 连接池。"""
    llm_adapter = StubLLMAdapter()  # 创建最小 LLM 适配器替身，避免容器装配触达真实模型适配器
    monkeypatch.setattr(  # 拦截容器内部真实适配器创建，改为返回测试替身
        litellm_adapter_module,
        "LiteLLMAdapter",
        lambda *args, **kwargs: llm_adapter,
    )
    _patch_container_redis(monkeypatch, fake_redis)  # 统一拦截主 Redis 与 pubsub Redis 创建，避免测试触碰真实 Redis
    monkeypatch.setattr(  # 拦截 MCP 管理器创建，确保本测试只验证容器启动预热语义
        Container,
        "_create_mcp_client_manager",
        staticmethod(lambda settings: StubMCPClientManager([])),
    )

    container = Container.create(settings=Settings(redis_url="redis://localhost:6379"))  # 创建容器，后续验证 startup 行为
    start_listener_calls = 0  # 记录聊天服务取消监听器的启动次数
    warmup_calls: list[tuple[object, int]] = []

    async def fake_start_cancel_listener() -> bool:
        """替换聊天服务监听器启动逻辑，只记录调用次数。"""
        nonlocal start_listener_calls
        start_listener_calls += 1
        return True

    async def fake_warmup(redis, target_connections: int = 150) -> None:
        """替换连接池预热逻辑，只记录传入参数。"""
        warmup_calls.append((redis, target_connections))

    monkeypatch.setattr(container.chat_service, "start_cancel_listener", fake_start_cancel_listener)  # 拦截监听器启动，避免测试里真的建立后台任务
    monkeypatch.setattr(Container, "_warmup_redis_pool", staticmethod(fake_warmup))  # 拦截连接池预热，转为纯记录行为

    await container.startup()  # 执行容器启动预热

    assert start_listener_calls == 1  # 断言 startup 会提前拉起全局取消监听器
    assert warmup_calls == [(fake_redis, 50)]  # 断言 startup 调用了主 Redis 连接池预热，使用容器实际传入的 target_connections=50


@pytest.mark.asyncio  # 标记为异步测试。
async def test_container_create_registers_mcp_tools_and_closes_manager(fake_redis, monkeypatch) -> None:
    """验证容器会注册 MCP 工具，并在关闭时回收管理器资源。"""
    llm_adapter = StubLLMAdapter()  # 创建最小 LLM 适配器替身。
    mcp_manager = StubMCPClientManager([StubMCPTool("mcp_fetch")])  # 创建包含一个 MCP 工具的管理器替身。
    monkeypatch.setattr(  # 拦截容器内部真实适配器创建，改为返回测试替身。
        litellm_adapter_module,
        "LiteLLMAdapter",
        lambda *args, **kwargs: llm_adapter,
    )
    _patch_container_redis(monkeypatch, fake_redis)  # 统一拦截主 Redis 与 pubsub Redis 创建，避免测试触碰真实 Redis。
    monkeypatch.setattr(Container, "_create_mcp_client_manager", staticmethod(lambda settings: mcp_manager))  # 拦截 MCP 管理器创建，改为返回测试替身。

    container = Container.create(  # 创建容器，验证 MCP 工具注册行为。
        settings=Settings(redis_url="redis://localhost:6379"),
    )

    tool_registry = container._agent_provider.get_default_profile().tool_registry  # 读取容器内部工具注册表。

    assert "mcp_fetch" in tool_registry  # 断言 MCP 工具已注册进工具注册表。

    await container.close()  # 关闭容器，验证资源回收行为。

    assert mcp_manager.closed is True  # 断言容器关闭时回收了 MCP 管理器资源。


@pytest.mark.asyncio  # 标记为异步测试。
async def test_container_create_fails_when_mcp_tool_names_conflict(fake_redis, monkeypatch) -> None:
    """验证多个 MCP 工具映射成同名时，容器创建会失败。"""
    llm_adapter = StubLLMAdapter()  # 创建最小 LLM 适配器替身。
    mcp_manager = StubMCPClientManager([StubMCPTool("mcp_fetch"), StubMCPTool("mcp_fetch")])  # 创建包含冲突工具名的管理器替身。
    monkeypatch.setattr(  # 拦截容器内部真实适配器创建，改为返回测试替身。
        litellm_adapter_module,
        "LiteLLMAdapter",
        lambda *args, **kwargs: llm_adapter,
    )
    _patch_container_redis(monkeypatch, fake_redis)  # 统一拦截主 Redis 与 pubsub Redis 创建，避免测试触碰真实 Redis。
    monkeypatch.setattr(Container, "_create_mcp_client_manager", staticmethod(lambda settings: mcp_manager))  # 拦截 MCP 管理器创建，改为返回测试替身。

    with pytest.raises(ValueError, match="mcp_fetch"):  # 断言容器创建因工具重名而失败。
        Container.create(settings=Settings(redis_url="redis://localhost:6379"))  # 执行容器创建，触发重复注册错误。


@pytest.mark.asyncio  # 标记为异步测试。
async def test_container_registers_task_tools(fake_redis, monkeypatch) -> None:
    """验证容器创建时会注册四个任务工具，且每个工具都持有非空的 task_service。"""
    llm_adapter = StubLLMAdapter()  # 创建最小 LLM 适配器替身。
    monkeypatch.setattr(  # 拦截容器内部真实适配器创建，改为返回测试替身。
        litellm_adapter_module,
        "LiteLLMAdapter",
        lambda *args, **kwargs: llm_adapter,
    )
    _patch_container_redis(monkeypatch, fake_redis)  # 统一拦截主 Redis 与 pubsub Redis 创建，避免测试触碰真实 Redis。
    monkeypatch.setattr(  # 拦截 MCP 管理器创建，避免该测试受外部 MCP 配置干扰。
        Container,
        "_create_mcp_client_manager",
        staticmethod(lambda settings: StubMCPClientManager([])),
    )

    container = Container.create(settings=Settings(redis_url="redis://localhost:6379"))  # 创建容器。

    tool_registry = container._agent_provider.get_default_profile().tool_registry  # 读取工具注册表。

    from app.infra.tools.plan_create_tool import PlanCreateTool
    from app.infra.tools.plan_get_tool import PlanGetTool
    from app.infra.tools.plan_list_tool import PlanListTool
    from app.infra.tools.query_tool_result_tool import QueryToolResultTool
    from app.infra.tools.plan_update_tool import PlanUpdateTool

    create_tool = tool_registry.get("plan_create")
    get_tool = tool_registry.get("plan_get")
    update_tool = tool_registry.get("plan_update")
    list_tool = tool_registry.get("plan_list")
    query_tool_result = tool_registry.get("query_tool_result")

    assert isinstance(create_tool, PlanCreateTool)  # 断言 plan_create 已注册且类型正确。
    assert isinstance(get_tool, PlanGetTool)  # 断言 plan_get 已注册且类型正确。
    assert isinstance(update_tool, PlanUpdateTool)  # 断言 plan_update 已注册且类型正确。
    assert isinstance(list_tool, PlanListTool)  # 断言 plan_list 已注册且类型正确。
    assert isinstance(query_tool_result, QueryToolResultTool)  # 断言 query_tool_result 已注册且类型正确。

    assert create_tool._task_service is not None  # 断言工具已注入 task_service。
    assert get_tool._task_service is not None
    assert update_tool._task_service is not None
    assert list_tool._task_service is not None


@pytest.mark.asyncio  # 标记为异步测试。
async def test_container_registered_task_tools_support_smoke_flow(fake_redis, monkeypatch) -> None:
    """验证容器注册的四个任务工具可经注册表取出并完成最小冒烟链路。"""
    llm_adapter = StubLLMAdapter()  # 创建最小 LLM 适配器替身。
    monkeypatch.setattr(  # 拦截容器内部真实适配器创建，改为返回测试替身。
        litellm_adapter_module,
        "LiteLLMAdapter",
        lambda *args, **kwargs: llm_adapter,
    )
    _patch_container_redis(monkeypatch, fake_redis)  # 统一拦截主 Redis 与 pubsub Redis 创建，避免测试触碰真实 Redis。
    monkeypatch.setattr(  # 拦截 MCP 管理器创建，确保本测试只覆盖任务工具注册与调用。
        Container,
        "_create_mcp_client_manager",
        staticmethod(lambda settings: StubMCPClientManager([])),
    )

    container = Container.create(settings=Settings(redis_url="redis://localhost:6379"))  # 创建容器。

    tool_registry = container._agent_provider.get_default_profile().tool_registry  # 读取容器内部工具注册表。
    create_tool = tool_registry.get("plan_create")  # 取出计划创建工具。
    get_tool = tool_registry.get("plan_get")  # 取出计划获取工具。
    update_tool = tool_registry.get("plan_update")  # 取出计划更新工具。
    list_tool = tool_registry.get("plan_list")  # 取出计划列表工具。
    context = _build_tool_context()  # 构造四个工具共享的执行上下文。

    assert create_tool is not None  # 断言计划创建工具已成功注册。
    assert get_tool is not None  # 断言计划获取工具已成功注册。
    assert update_tool is not None  # 断言计划更新工具已成功注册。
    assert list_tool is not None  # 断言计划列表工具已成功注册。

    create_result = await create_tool.call(  # 先通过创建工具写入一条任务。
        {
            "subject": "冒烟任务",  # 设置任务标题。
            "description": "验证容器注册表中的任务工具可以被串行调用",  # 设置任务描述。
            "activeForm": "正在执行冒烟测试",  # 设置进行时描述。
        },
        context,
    )
    assert create_result.is_error is False  # 断言创建阶段成功。
    created_task = json.loads(create_result.content)  # 解析创建结果。
    assert created_task["id"] == "1"  # 断言首个任务 ID 正确。

    get_result = await get_tool.call({"taskId": created_task["id"]}, context)  # 读取刚创建的任务详情。
    assert get_result.is_error is False  # 断言获取阶段成功。
    fetched_task = json.loads(get_result.content)  # 解析任务详情。
    assert fetched_task["subject"] == "冒烟任务"  # 断言获取到正确任务。

    update_result = await update_tool.call(  # 将任务状态更新为处理中。
        {"taskId": created_task["id"], "status": "in_progress"},
        context,
    )
    assert update_result.is_error is False  # 断言更新阶段成功。
    updated_task = json.loads(update_result.content)  # 解析更新结果。
    assert updated_task["status"] == "in_progress"  # 断言状态更新生效。

    list_result = await list_tool.call({}, context)  # 列出当前会话中的任务摘要。
    assert list_result.is_error is False  # 断言列表阶段成功。
    listed_tasks = json.loads(list_result.content)  # 解析任务摘要列表。
    assert len(listed_tasks) == 1  # 断言当前会话只存在一条任务。
    assert listed_tasks[0]["id"] == created_task["id"]  # 断言列表返回了刚创建的任务。
    assert listed_tasks[0]["status"] == "in_progress"  # 断言列表中也能观察到最新状态。


def _write_skill_file(project_root: Path, skill_name: str, description: str, body: str) -> None:
    """为容器测试写入一个最小 skill 文件。"""
    skill_dir = project_root / "skills" / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill_name}\ndescription: {description}\n---\n{body}\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_container_registers_skill_tool_from_root_skills_directory(fake_redis, monkeypatch, tmp_path: Path) -> None:
    """验证容器会扫描根级 skills 目录并注册 skill 工具。"""
    llm_adapter = StubLLMAdapter()
    monkeypatch.setattr(
        litellm_adapter_module,
        "LiteLLMAdapter",
        lambda *args, **kwargs: llm_adapter,
    )
    _patch_container_redis(monkeypatch, fake_redis)
    monkeypatch.setattr(
        Container,
        "_create_mcp_client_manager",
        staticmethod(lambda settings: StubMCPClientManager([])),
    )
    _write_skill_file(tmp_path, "demo", "演示技能", "skill body")
    monkeypatch.chdir(tmp_path)

    container = Container.create(settings=Settings(redis_url="redis://localhost:6379"))

    tool_registry = container._agent_provider.get_default_profile().tool_registry
    skill_tool = tool_registry.get("skill")

    assert skill_tool is not None
    assert "plan_create" in tool_registry


@pytest.mark.asyncio
async def test_container_registers_run_python_script_tool(fake_redis, monkeypatch, tmp_path: Path) -> None:
    """验证容器创建时会注册 run_python_script 工具，且注入参数正确。"""
    llm_adapter = StubLLMAdapter()
    monkeypatch.setattr(
        litellm_adapter_module,
        "LiteLLMAdapter",
        lambda *args, **kwargs: llm_adapter,
    )
    _patch_container_redis(monkeypatch, fake_redis)
    monkeypatch.setattr(
        Container,
        "_create_mcp_client_manager",
        staticmethod(lambda settings: StubMCPClientManager([])),
    )
    monkeypatch.chdir(tmp_path)

    settings = Settings(redis_url="redis://localhost:6379")
    container = Container.create(settings=settings)

    tool_registry = container._agent_provider.get_default_profile().tool_registry
    python_tool = tool_registry.get("run_python_script")

    from app.infra.tools.run_python_script_tool import RunPythonScriptTool

    assert python_tool is not None  # 断言工具已注册
    assert isinstance(python_tool, RunPythonScriptTool)  # 断言实例类型正确
    assert python_tool._workspace_root == settings.workspace_root  # 断言 workspace_root 注入正确

    llm_tools = tool_registry.to_llm_tools()
    tool_names = [t["function"]["name"] for t in llm_tools]
    assert "run_python_script" in tool_names  # 断言 schema 暴露正常
    run_python_script_def = next(t for t in llm_tools if t["function"]["name"] == "run_python_script")
    assert run_python_script_def["function"]["description"]  # 断言描述非空
    assert run_python_script_def["function"]["parameters"]  # 断言参数 schema 非空


@pytest.mark.asyncio
async def test_container_registers_list_resumable_subagents_tool(fake_redis, monkeypatch) -> None:
    """验证容器注册表中包含 ListResumableSubagents 工具。"""
    llm_adapter = StubLLMAdapter()
    monkeypatch.setattr(
        litellm_adapter_module,
        "LiteLLMAdapter",
        lambda *args, **kwargs: llm_adapter,
    )
    _patch_container_redis(monkeypatch, fake_redis)
    monkeypatch.setattr(
        Container,
        "_create_mcp_client_manager",
        staticmethod(lambda settings: StubMCPClientManager([])),
    )

    container = Container.create(settings=Settings(redis_url="redis://localhost:6379"))

    tool_registry = container._agent_provider.get_default_profile().tool_registry

    from app.infra.tools.list_resumable_subagents_tool import ListResumableSubagentsTool

    list_tool = tool_registry.get("ListResumableSubagents")
    assert list_tool is not None
    assert isinstance(list_tool, ListResumableSubagentsTool)
