"""ChatService 单元测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from app.services.chat_service import ChatService
from app.services.agent_provider import AgentProvider
from app.infra.store.redis_session_store import RedisSessionStore
from app.infra.store.redis_run_store import RedisRunStore
from app.infra.store.redis_lock_store import RedisLockStore
from app.core.models.session import Session
from app.core.models.agent import Agent, AgentExecutionProfile, AgentPromptSource
from app.core.hooks import ToolHookPipeline
from app.core.models.run import Run, RunStatus
from app.core.models.stored_message import StoredMessage
from app.core.models.event import (
    RunStartedEvent,
    MessageDeltaEvent,
    RunCompletedEvent,
    RunCancelledEvent,
    RunFailedEvent,
    RequestFailedEvent,
    ToolUseCompletedEvent,
)
from app.core.models.error import ErrorCode
from app.core.models.tool import ToolRegistry
from app.core.loop.agent_loop import AgentLoop
from app.config import Settings
from app.core.runtime.context_builder import ContextCompressionError, ContextTrimPolicy
from tests.fakes import FakeAgentRuntime


class FakeAgentProvider(AgentProvider):
    """模拟 Agent 提供者（v2 多 profile 版本）。

    同时提供 Agent 静态配置访问和执行 profile 访问，
    兼容旧的 get_default() 和新的 get_default_profile() 方法。
    """

    def __init__(self, profile: AgentExecutionProfile | None = None) -> None:  # 构造函数
        """初始化模拟提供者。

        Args:
            profile: 预设的执行 profile；未提供时自动构造默认 profile
        """
        if profile is not None:  # 使用外部注入的 profile
            self.profile = profile  # 保存外部注入的 profile
        else:
            agent = Agent(  # 创建默认 Agent 静态配置
                agent_id="master-agent",
                name="Master Agent",
                model="gpt-4.1-mini",
                system_prompt="你是一个乐于助人的助手。",
                temperature=0.2,
            )
            self.profile = AgentExecutionProfile(  # 构造默认执行 profile
                agent_id=agent.agent_id,
                agent=agent,
                prompt_source=AgentPromptSource(kind="file", path="master_prompt.md"),
                runtime=FakeAgentRuntime(),  # 使用空的模拟 Runtime
                tool_registry=ToolRegistry(),  # 空工具注册表
                tool_hook_pipeline=ToolHookPipeline(),  # 空 Hook 管线
                max_turns=10,
            )

    def get_default(self) -> Agent:  # 获取默认 Agent
        """返回默认 Agent 的静态配置。"""
        return self.profile.agent

    def get_default_profile(self) -> AgentExecutionProfile:  # 获取默认执行 profile
        """返回默认主 Agent 的执行 profile。"""
        return self.profile

    def get_profile(self, agent_id: str) -> AgentExecutionProfile:  # 按 ID 获取 profile
        """按 agent_id 获取对应的执行 profile。"""
        if agent_id != self.profile.agent_id:  # 非默认 profile 的 agent_id 视为未注册
            raise ValueError(agent_id)
        return self.profile

    def get_child_profile(self, subagent_type: str) -> AgentExecutionProfile:  # 获取子代理 profile
        """按子代理类型获取执行 profile，测试中无子代理。"""
        raise ValueError(subagent_type)  # 测试场景不注册子代理，直接抛错误

    def get_sub_agents(self) -> list[Agent]:  # 获取子智能体
        """返回子智能体列表。"""
        return []  # 测试场景无子代理


@pytest.fixture  # 定义 pytest 夹具
async def chat_service(fake_redis):  # ChatService 夹具
    """提供配置好的 ChatService 实例。"""
    session_store = RedisSessionStore(fake_redis, key_prefix="test")  # 创建会话存储
    run_store = RedisRunStore(fake_redis, key_prefix="test")  # 创建 Run 存储
    lock_store = RedisLockStore(fake_redis, key_prefix="test")  # 创建锁存储

    # 创建一个成功的 FakeAgentRuntime
    fake_runtime = FakeAgentRuntime(  # 创建模拟 Runtime
        events=[
            RunStartedEvent(run_id="run-1", session_id="session-1"),
            MessageDeltaEvent(run_id="run-1", content="Hello"),
            MessageDeltaEvent(run_id="run-1", content=" World"),
            RunCompletedEvent(run_id="run-1", output="Hello World"),
        ]
    )

    # 创建工具注册表和 AgentLoop（无状态设计，不再注入 runtime/tool_registry）
    tool_registry = ToolRegistry()  # 创建工具注册表
    agent_loop = AgentLoop(default_max_turns=10)  # 创建 Agent 循环，运行依赖通过 profile 注入

    # 构造 master profile，将 fake_runtime 注入其中
    agent = Agent(  # 创建默认 Agent 静态配置
        agent_id="master-agent",
        name="Master Agent",
        model="gpt-4.1-mini",
        system_prompt="你是一个乐于助人的助手。",
        temperature=0.2,
    )
    profile = AgentExecutionProfile(  # 构造执行 profile
        agent_id=agent.agent_id,
        agent=agent,
        prompt_source=AgentPromptSource(kind="file", path="master_prompt.md"),
        runtime=fake_runtime,  # 注入模拟 Runtime
        tool_registry=tool_registry,  # 注入工具注册表
        tool_hook_pipeline=ToolHookPipeline(),  # 空 Hook 管线
        max_turns=10,
    )
    agent_provider = FakeAgentProvider(profile=profile)  # 注入 profile 的提供者

    # 创建测试用的 Settings，设置锁 TTL 为 30 秒
    settings = Settings(  # 创建设置实例
        redis_url="redis://localhost:6379",
        session_lock_ttl_seconds=30,  # 设置锁 TTL 为 30 秒
    )

    service = ChatService(  # 创建服务实例
        session_store=session_store,
        run_store=run_store,
        lock_store=lock_store,
        agent_provider=agent_provider,  # 注入带 profile 的 Agent 提供者
        agent_loop=agent_loop,  # 注入 Agent 循环（无状态设计）
        settings=settings,  # 注入应用配置
        redis=fake_redis,  # 注入 Redis 客户端
        # extra_system_messages 不再由构造注入，运行时从 master profile 读取
    )
    # 保存 fake_runtime 以便测试中检查
    service._fake_runtime = fake_runtime  # 附加到服务实例
    try:
        yield service  # 将服务实例提供给测试，并在测试结束后统一回收后台监听器
    finally:
        await service.aclose()  # 主动关闭全局取消监听器，避免后台任务残留到下一个测试


@pytest.fixture  # 定义 pytest 夹具
async def session_with_history(fake_redis):  # 带历史消息的会话夹具
    """提供带有历史消息的会话。"""
    session_store = RedisSessionStore(fake_redis, key_prefix="test")  # 创建会话存储

    # 创建会话
    session = Session(  # 构造会话实例
        session_id="session-1",
        agent_id="default-agent",
        created_at=datetime.now(timezone.utc),
    )
    await session_store.create_session(session)  # 创建会话记录

    # 添加历史消息
    await session_store.append_message(  # 添加旧的用户消息
        session.session_id,
        StoredMessage.create(
            role="user",
            content="Previous question",
            timestamp=datetime.now(timezone.utc),
        ),
    )
    await session_store.append_message(  # 添加旧的助手回复
        session.session_id,
        StoredMessage.create(
            role="assistant",
            content="Previous answer",
            timestamp=datetime.now(timezone.utc),
        ),
    )

    return session  # 返回会话


@pytest.mark.asyncio  # 标记异步测试
async def test_chat_service_returns_request_failed_for_missing_session(chat_service):  # 测试会话不存在
    """测试当会话不存在时应返回 request_failed 事件。"""
    events = [event async for event in chat_service.stream_chat("non-existent", "hi")]  # 流式聊天

    assert len(events) == 1  # 验证只有一个事件
    assert events[0].event_name == "request_failed"  # 验证是 request_failed 事件
    assert events[0].error_code == ErrorCode.SESSION_NOT_FOUND  # 验证错误码正确


@pytest.mark.asyncio  # 标记异步测试
async def test_chat_service_persists_terminal_state_before_emitting_run_completed(
    chat_service, session_with_history, fake_redis
):  # 测试终态持久化顺序
    """测试 ChatService 必须在发出 run_completed 之前先持久化终态。"""
    # 收集所有事件
    events = [event async for event in chat_service.stream_chat("session-1", "hi")]  # 流式聊天

    # 验证事件顺序
    assert [event.event_name for event in events] == ["run_started", "message_delta", "message_delta", "run_completed"]  # 验证事件顺序

    # 从事件中获取实际使用的 run_id
    actual_run_id = events[0].run_id  # 从第一个事件（run_started）获取 run_id

    # 验证 Run 被持久化为 COMPLETED
    run_store = RedisRunStore(fake_redis, key_prefix="test")  # 创建 Run 存储
    persisted_run = await run_store.get_run(actual_run_id)  # 查询持久化的 Run
    assert persisted_run is not None  # 验证 Run 已持久化
    assert persisted_run.status == RunStatus.COMPLETED  # 验证状态为已完成
    assert persisted_run.output == "Hello World"  # 验证输出内容正确

    # 验证助手消息被追加到会话
    session_store = RedisSessionStore(fake_redis, key_prefix="test")  # 创建会话存储
    messages = await session_store.list_messages("session-1")  # 查询消息列表
    # 应该是：2条历史 + 1条新用户消息 + 1条助手回复 = 4条
    assert len(messages) == 4  # 验证消息数量为4条
    assert messages[-1].role == "assistant"  # 验证最后一条是助手消息
    assert messages[-1].content == "Hello World"  # 验证助手消息内容


@pytest.mark.asyncio  # 标记异步测试
async def test_chat_service_stream_chat_sets_run_ttl(chat_service, session_with_history, fake_redis):
    """测试主聊天链路创建 run 时会写入配置的 TTL。"""
    events = [event async for event in chat_service.stream_chat("session-1", "hi")]

    actual_run_id = events[0].run_id
    ttl = await fake_redis.ttl(f"test:run:{actual_run_id}")

    assert ttl > 0
    assert ttl <= chat_service._settings.run_ttl_seconds


@pytest.mark.asyncio  # 标记异步测试
async def test_chat_service_persist_terminal_completed_uses_single_pipeline_execute(
    chat_service, session_with_history, fake_redis, monkeypatch
):
    """测试完成态的 Run 更新与 assistant 成稿会合并到一次 pipeline.execute()。"""
    execute_calls = 0  # 记录 pipeline.execute 调用次数，验证完成态双写只往返一次 Redis
    original_pipeline = fake_redis.pipeline  # 保存原始 pipeline 工厂，便于继续复用 fakeredis 的真实行为

    class RecordingPipeline:
        """包装真实 pipeline，额外记录 execute 次数。"""

        def __init__(self, inner) -> None:
            """保存被包装的真实 pipeline。"""
            self._inner = inner

        def hset(self, *args, **kwargs):
            """透传 HSET 命令到真实 pipeline。"""
            self._inner.hset(*args, **kwargs)
            return self

        def rpush(self, *args, **kwargs):
            """透传 RPUSH 命令到真实 pipeline。"""
            self._inner.rpush(*args, **kwargs)
            return self

        async def execute(self):
            """记录 execute 次数后执行真实 pipeline。"""
            nonlocal execute_calls
            execute_calls += 1
            return await self._inner.execute()

    monkeypatch.setattr(fake_redis, "pipeline", lambda: RecordingPipeline(original_pipeline()))

    run = Run(
        run_id="run-terminal-completed",
        session_id=session_with_history.session_id,
        status=RunStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
    )
    await chat_service._run_store.create_run(run)

    await chat_service._persist_terminal_state(
        session_id=session_with_history.session_id,
        run_id=run.run_id,
        terminal_event=RunCompletedEvent(run_id=run.run_id, output="done"),
        final_output="done",
    )

    assert execute_calls == 1  # 完成态应只执行一次 pipeline
    persisted_run = await chat_service._run_store.get_run(run.run_id)
    assert persisted_run is not None
    assert persisted_run.status == RunStatus.COMPLETED
    assert persisted_run.output == "done"

    session_store = RedisSessionStore(fake_redis, key_prefix="test")
    messages = await session_store.list_messages(session_with_history.session_id)
    assert messages[-1].role == "assistant"
    assert messages[-1].content == "done"


@pytest.mark.asyncio  # 标记异步测试
async def test_chat_service_persist_terminal_cancelled_uses_single_pipeline_execute(
    chat_service, session_with_history, fake_redis, monkeypatch
):
    """测试取消态的 Run 更新与取消提示消息会合并到一次 pipeline.execute()。"""
    execute_calls = 0  # 记录 pipeline.execute 次数，验证取消态双写同样只往返一次 Redis
    original_pipeline = fake_redis.pipeline

    class RecordingPipeline:
        """包装真实 pipeline，额外记录 execute 次数。"""

        def __init__(self, inner) -> None:
            """保存被包装的真实 pipeline。"""
            self._inner = inner

        def hset(self, *args, **kwargs):
            """透传 HSET 命令到真实 pipeline。"""
            self._inner.hset(*args, **kwargs)
            return self

        def rpush(self, *args, **kwargs):
            """透传 RPUSH 命令到真实 pipeline。"""
            self._inner.rpush(*args, **kwargs)
            return self

        async def execute(self):
            """记录 execute 次数后执行真实 pipeline。"""
            nonlocal execute_calls
            execute_calls += 1
            return await self._inner.execute()

    monkeypatch.setattr(fake_redis, "pipeline", lambda: RecordingPipeline(original_pipeline()))

    run = Run(
        run_id="run-terminal-cancelled",
        session_id=session_with_history.session_id,
        status=RunStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
    )
    await chat_service._run_store.create_run(run)

    await chat_service._persist_terminal_state(
        session_id=session_with_history.session_id,
        run_id=run.run_id,
        terminal_event=RunCancelledEvent(
            run_id=run.run_id,
            reason="cancelled",
            error_code=ErrorCode.RUN_CANCELLED,
        ),
        final_output="",
    )

    assert execute_calls == 1  # 取消态也应只执行一次 pipeline
    persisted_run = await chat_service._run_store.get_run(run.run_id)
    assert persisted_run is not None
    assert persisted_run.status == RunStatus.CANCELLED
    assert persisted_run.error_code == ErrorCode.RUN_CANCELLED

    session_store = RedisSessionStore(fake_redis, key_prefix="test")
    messages = await session_store.list_messages(session_with_history.session_id)
    assert messages[-1].role == "system"
    assert messages[-1].content == "此次生成已被用户取消。"
    assert messages[-1].is_meta is True


@pytest.mark.asyncio  # 标记异步测试
async def test_chat_service_builds_execution_context_and_passes_it_to_runtime(
    chat_service, session_with_history
):  # 测试 metadata 参数
    """测试 ChatService 会构造 ExecutionContext 并透传给 Runtime。"""
    # 收集所有事件
    events = [event async for event in chat_service.stream_chat("session-1", "hi", metadata={"trace_id": "req-1"})]  # 带 metadata 的流式聊天

    assert events[0].event_name == "run_started"  # 验证运行开始

    # 验证 Runtime 收到执行上下文，metadata 被包进 context 而不是散落在顶层参数里
    fake_runtime = chat_service._fake_runtime  # 获取模拟 Runtime
    assert "metadata" not in fake_runtime.last_call  # 验证 metadata 未传递给 Runtime
    assert fake_runtime.last_call["context"] is not None  # 验证上下文已传递
    assert fake_runtime.last_call["context"].metadata == {"trace_id": "req-1"}  # 验证 metadata 已进入上下文
    assert fake_runtime.last_call["context"].session_id == "session-1"  # 验证会话 ID 正确
    assert fake_runtime.last_call["context"].agent.agent_id == "master-agent"  # 验证 Agent 已进入上下文
    # 验证消息内容正确
    assert fake_runtime.last_call["messages"][-1]["content"] == "hi"  # 验证最后一条消息内容


@pytest.mark.asyncio  # 标记异步测试
async def test_chat_service_releases_lock_after_completion(chat_service, session_with_history, fake_redis):  # 测试锁释放
    """测试 ChatService 完成后应释放会话锁。"""
    lock_store = RedisLockStore(fake_redis, key_prefix="test")  # 创建锁存储

    # 收集所有事件
    events = [event async for event in chat_service.stream_chat("session-1", "hi")]  # 流式聊天

    assert events[-1].event_name == "run_completed"  # 验证最后一个事件是完成

    # 验证锁已释放
    active_run_id = await lock_store.get_active_run_id("session-1")  # 查询活跃 Run ID
    assert active_run_id is None  # 验证锁已释放
    assert chat_service._active_cancel_events == {}  # 验证 run 结束后本地取消映射已被清理，避免活跃 run 表泄漏


@pytest.mark.asyncio
async def test_chat_service_global_cancel_listener_sets_matching_local_event(chat_service, fake_redis):
    """测试全局模式订阅监听器收到 run_cancel 消息后，会命中并触发本地取消事件。"""
    started = await chat_service.start_cancel_listener()  # 显式启动全局监听器，覆盖独立模式订阅路径
    assert started is True  # 监听器应能成功启动

    cancel_event = asyncio.Event()  # 构造一个本地取消事件，模拟当前 worker 正在执行的 run
    chat_service._active_cancel_events["run-listener"] = cancel_event  # 手动登记到活跃 run 表，便于监听器命中

    try:
        await fake_redis.publish("run_cancel:run-listener", "cancel")  # 向模式频道发布取消消息，模拟其他 worker 的取消广播
        await asyncio.wait_for(cancel_event.wait(), timeout=1)  # 等待监听器命中本地 run 并设置取消事件
        assert cancel_event.is_set() is True  # 断言本地取消事件已被全局监听器正确触发
    finally:
        chat_service._active_cancel_events.pop("run-listener", None)  # 清理测试手动登记的 run，避免影响后续测试


@pytest.mark.asyncio
async def test_chat_service_start_cancel_listener_is_idempotent(chat_service):
    """测试重复启动全局取消监听器时会复用同一个后台任务。"""
    first_started = await chat_service.start_cancel_listener()  # 首次启动监听器，建立共享 pubsub 长连接与后台任务
    first_task = chat_service._cancel_listener_task  # 记录首次启动得到的后台任务引用
    second_started = await chat_service.start_cancel_listener()  # 第二次启动应直接复用，不再重复创建后台任务

    assert first_started is True  # 首次启动应成功
    assert second_started is True  # 第二次启动同样应返回可用状态
    assert first_task is not None  # 首次启动后应已有后台监听任务
    assert chat_service._cancel_listener_task is first_task  # 断言两次启动复用的是同一条后台监听任务


@pytest.mark.asyncio
async def test_chat_service_cancel_run_prefers_local_event_over_redis_publish(chat_service, monkeypatch):
    """测试本地命中活跃 run 时，cancel_run 会直接触发事件而不是广播到 Redis。"""
    publish_called = False  # 记录 Redis publish 是否被调用，验证本地取消优先级

    async def fake_publish(channel: str, message: str) -> int:
        """替换 Redis publish，若被调用则更新标记。"""
        nonlocal publish_called
        publish_called = True
        return 1

    monkeypatch.setattr(chat_service._redis, "publish", fake_publish)  # 拦截 Redis 广播，避免测试误依赖真实 publish 行为
    local_event = asyncio.Event()  # 构造本地取消事件，模拟 run 仍在当前 worker 活跃执行
    chat_service._active_cancel_events["run-local"] = local_event  # 手动登记本地活跃 run，覆盖 cancel_run 的快速路径

    try:
        assert chat_service.cancel_run("run-local") is True  # 发起取消请求，应命中本地事件并立即返回成功
        await asyncio.sleep(0)  # 让事件循环切换一次，确保如果错误创建了 publish 任务也能暴露出来
        assert local_event.is_set() is True  # 断言本地取消事件已经被直接触发
        assert publish_called is False  # 断言本地命中时没有再走 Redis 广播
    finally:
        chat_service._active_cancel_events.pop("run-local", None)  # 清理测试手动登记的 run，避免影响后续测试


@pytest.mark.asyncio
async def test_chat_service_cancel_run_publishes_when_run_is_not_local(chat_service, monkeypatch):
    """测试本地未命中活跃 run 时，cancel_run 会通过 Redis 广播取消信号。"""
    published_messages: list[tuple[str, str]] = []

    async def fake_publish(channel: str, message: str) -> int:
        """替换 Redis publish，记录广播参数。"""
        published_messages.append((channel, message))
        return 1

    monkeypatch.setattr(chat_service._redis, "publish", fake_publish)  # 拦截 Redis 广播，转为内存记录

    assert chat_service.cancel_run("run-remote") is True  # 本地无活跃 run 时，仍应返回“已发出取消信号”
    await asyncio.sleep(0)  # 等待后台 create_task 调度执行 fake_publish

    assert published_messages == [("run_cancel:run-remote", "cancel")]  # 断言广播频道与消息体符合当前取消协议


@pytest.mark.asyncio  # 标记异步测试
async def test_chat_service_returns_request_failed_when_session_locked(
    fake_redis, session_with_history
):  # 测试会话冲突
    """测试当会话已被锁定时应返回 request_failed 事件。"""
    lock_store = RedisLockStore(fake_redis, key_prefix="test")  # 创建锁存储
    await lock_store.acquire("session-1", "existing-run", ttl_seconds=30)  # 先占用锁

    # 创建服务（使用假的 agent_loop）
    fake_runtime = FakeAgentRuntime(events=[])  # 创建空的模拟 Runtime
    tool_registry = ToolRegistry()  # 创建工具注册表
    agent_loop = AgentLoop(default_max_turns=10)  # 创建 Agent 循环（无状态设计）
    settings = Settings(  # 创建设置实例
        redis_url="redis://localhost:6379",
        session_lock_ttl_seconds=30,  # 设置锁 TTL 为 30 秒
    )
    service = ChatService(  # 创建服务实例
        session_store=RedisSessionStore(fake_redis, key_prefix="test"),
        run_store=RedisRunStore(fake_redis, key_prefix="test"),
        lock_store=lock_store,
        agent_provider=FakeAgentProvider(),
        agent_loop=agent_loop,  # 注入 Agent 循环
        settings=settings,  # 注入应用配置
        redis=fake_redis,  # 注入 Redis 客户端
    )

    # 收集所有事件
    events = [event async for event in service.stream_chat("session-1", "hi")]  # 流式聊天

    assert len(events) == 1  # 验证只有一个事件
    assert events[0].event_name == "request_failed"  # 验证是 request_failed 事件
    assert events[0].error_code == ErrorCode.SESSION_RUN_CONFLICT  # 验证错误码是会话冲突


@pytest.mark.asyncio  # 标记异步测试
async def test_chat_service_emits_run_failed_and_persists_failed_state(
    fake_redis, session_with_history
):  # 测试失败流程
    """测试当 Runtime 返回 run_failed 时，应持久化失败状态并发出事件。"""
    # 创建返回 run_failed 的 FakeAgentRuntime
    fake_runtime = FakeAgentRuntime(  # 创建返回失败的模拟 Runtime
        events=[
            RunStartedEvent(run_id="run-1", session_id="session-1"),
            RunFailedEvent(run_id="run-1", error_code=ErrorCode.LLM_REQUEST_FAILED, message="LLM error"),
        ]
    )

    tool_registry = ToolRegistry()  # 创建工具注册表
    agent_loop = AgentLoop(default_max_turns=10)  # 创建 Agent 循环（无状态设计）

    # 将 fake_runtime 注入到 profile，确保 AgentLoop 使用正确的模拟 Runtime
    agent = Agent(  # 创建默认 Agent 静态配置
        agent_id="master-agent",
        name="Master Agent",
        model="gpt-4.1-mini",
        system_prompt="你是一个乐于助人的助手。",
        temperature=0.2,
    )
    profile = AgentExecutionProfile(  # 构造执行 profile
        agent_id=agent.agent_id,
        agent=agent,
        prompt_source=AgentPromptSource(kind="file", path="master_prompt.md"),
        runtime=fake_runtime,  # 注入返回 run_failed 的模拟 Runtime
        tool_registry=tool_registry,
        tool_hook_pipeline=ToolHookPipeline(),
        max_turns=10,
    )

    settings = Settings(  # 创建设置实例
        redis_url="redis://localhost:6379",
        session_lock_ttl_seconds=30,  # 设置锁 TTL 为 30 秒
    )
    service = ChatService(  # 创建服务实例
        session_store=RedisSessionStore(fake_redis, key_prefix="test"),
        run_store=RedisRunStore(fake_redis, key_prefix="test"),
        lock_store=RedisLockStore(fake_redis, key_prefix="test"),
        agent_provider=FakeAgentProvider(profile=profile),  # 注入带失败事件的 profile
        agent_loop=agent_loop,  # 注入 Agent 循环
        settings=settings,  # 注入应用配置
        redis=fake_redis,  # 注入 Redis 客户端
    )

    # 收集所有事件
    events = [event async for event in service.stream_chat("session-1", "hi")]  # 流式聊天

    # 验证事件顺序
    assert [event.event_name for event in events] == ["run_started", "run_failed"]  # 验证事件顺序

    # 从事件中获取实际使用的 run_id
    actual_run_id = events[0].run_id  # 从第一个事件（run_started）获取 run_id

    # 验证 Run 被持久化为 FAILED
    run_store = RedisRunStore(fake_redis, key_prefix="test")  # 创建 Run 存储
    persisted_run = await run_store.get_run(actual_run_id)  # 查询持久化的 Run
    assert persisted_run is not None  # 验证 Run 已持久化
    assert persisted_run.status == RunStatus.FAILED  # 验证状态为失败
    assert persisted_run.error_code == ErrorCode.LLM_REQUEST_FAILED  # 验证错误码正确

    # 验证失败时不写入助手消息（只有用户消息）
    session_store = RedisSessionStore(fake_redis, key_prefix="test")  # 创建会话存储
    messages = await session_store.list_messages("session-1")  # 查询消息列表
    # 只有 3 条：2条历史 + 1条新用户消息（run_failed 不写 assistant）
    assert len(messages) == 3  # 验证消息数量


@pytest.mark.asyncio  # 标记异步测试
async def test_chat_service_appends_user_message_before_runtime(fake_redis, session_with_history):  # 测试用户消息持久化
    """测试 ChatService 应在调用 Runtime 之前先持久化用户消息。"""
    # 创建记录调用顺序的 FakeAgentRuntime
    call_order = []  # 记录调用顺序

    class RecordingFakeRuntime:  # 记录调用顺序的模拟 Runtime
        def __init__(self) -> None:  # 构造函数
            self.last_call: dict[str, Any] | None = None  # 记录最后一次调用

        async def stream(self, agent, run, messages):  # 模拟流式执行（向后兼容）
            call_order.append("runtime_stream")  # 记录 Runtime 被调用
            self.last_call = {"agent": agent, "run": run, "messages": messages}  # 记录调用参数
            yield RunStartedEvent(run_id=run.run_id, session_id=run.session_id)  # 生成开始事件
            yield RunCompletedEvent(run_id=run.run_id, output="Done")  # 生成完成事件

        async def stream_once(self, agent, messages, tools=None, context=None):  # 模拟单次流式调用
            from app.core.runtime.agent_runtime import TurnComplete
            call_order.append("runtime_stream")  # 记录 Runtime 被调用
            self.last_call = {"agent": agent, "messages": messages, "tools": tools, "context": context}  # 记录调用参数
            yield "Done"  # 生成文本增量
            yield TurnComplete(tool_calls=None, usage=None)  # 生成最终结果

    fake_runtime = RecordingFakeRuntime()  # 创建记录 Runtime

    # 创建自定义 session_store 来记录调用顺序
    session_store = RedisSessionStore(fake_redis, key_prefix="test")  # 创建会话存储
    original_append = session_store.append_main_message  # 保存新的主会话上下文写入方法
    original_list_active_messages_with_indices = session_store.list_active_main_messages_with_indices  # 保存新的主会话活动窗口查询方法

    async def recording_append(session_id, message, source_run_id=None):  # 记录调用的 append
        call_order.append("append_message")  # 记录消息追加
        return await original_append(session_id, message, source_run_id=source_run_id)  # 调用原始方法

    async def recording_list_active_messages_with_indices(session_id):  # 记录活动窗口查询调用
        call_order.append("list_active_messages_with_indices")  # 记录活动窗口查询
        return await original_list_active_messages_with_indices(session_id)  # 调用原始活动窗口查询方法

    session_store.append_main_message = recording_append  # 替换新的主会话上下文写入方法
    session_store.list_active_main_messages_with_indices = recording_list_active_messages_with_indices  # 替换新的主会话活动窗口查询方法

    tool_registry = ToolRegistry()  # 创建工具注册表
    agent_loop = AgentLoop(default_max_turns=10)  # 创建 Agent 循环（无状态设计）

    # 将 RecordingFakeRuntime 注入到 profile，确保 AgentLoop 使用正确的模拟 Runtime
    agent = Agent(  # 创建默认 Agent 静态配置
        agent_id="master-agent",
        name="Master Agent",
        model="gpt-4.1-mini",
        system_prompt="你是一个乐于助人的助手。",
        temperature=0.2,
    )
    profile = AgentExecutionProfile(  # 构造执行 profile
        agent_id=agent.agent_id,
        agent=agent,
        prompt_source=AgentPromptSource(kind="file", path="master_prompt.md"),
        runtime=fake_runtime,  # 注入 RecordingFakeRuntime
        tool_registry=tool_registry,
        tool_hook_pipeline=ToolHookPipeline(),
        max_turns=10,
    )

    settings = Settings(  # 创建设置实例
        redis_url="redis://localhost:6379",
        session_lock_ttl_seconds=30,  # 设置锁 TTL 为 30 秒
    )
    service = ChatService(  # 创建服务实例
        session_store=session_store,
        run_store=RedisRunStore(fake_redis, key_prefix="test"),
        lock_store=RedisLockStore(fake_redis, key_prefix="test"),
        agent_provider=FakeAgentProvider(profile=profile),  # 注入带 RecordingFakeRuntime 的 profile
        agent_loop=agent_loop,  # 注入 Agent 循环
        settings=settings,  # 注入应用配置
        redis=fake_redis,  # 注入 Redis 客户端
    )

    # 执行
    events = [event async for event in service.stream_chat("session-1", "test message")]  # 流式聊天

    # 验证新的调用顺序：先读旧历史，再持久化当前用户消息，最后再调用 Runtime。
    assert call_order[:3] == ["list_active_messages_with_indices", "append_message", "runtime_stream"]  # 验证上下文准备顺序

    # 验证消息已持久化
    messages = await session_store.list_messages("session-1")  # 查询消息列表
    assert any(m.content == "test message" for m in messages)  # 验证用户消息已保存

    # 验证传给 Runtime 的最后一条消息就是当前用户消息，且格式已是 LLM 需要的 dict。
    assert fake_runtime.last_call is not None  # 验证 Runtime 被调用
    assert fake_runtime.last_call["messages"][-1] == {"role": "user", "content": "test message"}  # 验证消息格式


@pytest.mark.asyncio  # 标记异步测试
async def test_chat_service_passes_injected_context_trim_policy_to_context_builder(
    fake_redis, session_with_history
):  # 测试上下文策略注入
    """测试 ChatService 应将注入的上下文策略传递给 ContextBuilder。"""
    class OnlyCurrentUserPolicy(ContextTrimPolicy):
        """仅保留 system 和当前用户消息的测试策略。"""

        def __init__(self) -> None:
            """初始化测试策略。"""
            self.called = False  # 记录策略是否被调用

        async def build_messages(
            self,
            *,
            agent: Agent,
            system_message: StoredMessage,
            history: list[StoredMessage],
            history_indices: list[int] | None = None,
            current_user_message: StoredMessage | None,
            session_id: str | None = None,
            summary_target=None,
            extra_system_messages: list[str] | None = None,
        ) -> list[StoredMessage]:
            """返回裁剪后的消息列表。"""
            del agent, history, history_indices, session_id, summary_target, extra_system_messages
            self.called = True  # 标记策略已被调用
            return [system_message, current_user_message]  # 仅保留 system 和当前用户消息

    fake_runtime = FakeAgentRuntime(
        events=[
            RunStartedEvent(run_id="run-1", session_id="session-1"),
            RunCompletedEvent(run_id="run-1", output="Done"),
        ]
    )

    tool_registry = ToolRegistry()  # 创建工具注册表
    agent_loop = AgentLoop(default_max_turns=10)  # 创建 Agent 循环（无状态设计）

    # 将 fake_runtime 注入到 profile，确保 AgentLoop 使用正确的模拟 Runtime
    agent = Agent(  # 创建默认 Agent 静态配置
        agent_id="master-agent",
        name="Master Agent",
        model="gpt-4.1-mini",
        system_prompt="你是一个乐于助人的助手。",
        temperature=0.2,
    )
    profile = AgentExecutionProfile(  # 构造执行 profile
        agent_id=agent.agent_id,
        agent=agent,
        prompt_source=AgentPromptSource(kind="file", path="master_prompt.md"),
        runtime=fake_runtime,  # 注入返回 RunCompleted 的模拟 Runtime
        tool_registry=tool_registry,
        tool_hook_pipeline=ToolHookPipeline(),
        max_turns=10,
    )

    policy = OnlyCurrentUserPolicy()  # 创建测试策略

    settings = Settings(
        redis_url="redis://localhost:6379",
        session_lock_ttl_seconds=30,
    )
    service = ChatService(
        session_store=RedisSessionStore(fake_redis, key_prefix="test"),
        run_store=RedisRunStore(fake_redis, key_prefix="test"),
        lock_store=RedisLockStore(fake_redis, key_prefix="test"),
        agent_provider=FakeAgentProvider(profile=profile),  # 注入带自定义事件的 profile
        agent_loop=agent_loop,  # 注入 Agent 循环
        settings=settings,
        context_trim_policy=policy,
        redis=fake_redis,  # 注入 Redis 客户端
    )

    events = [event async for event in service.stream_chat("session-1", "hi")]  # 流式聊天

    assert events[-1].event_name == "run_completed"  # 验证流程正常完成
    assert policy.called is True  # 验证注入的策略确实被 ContextBuilder 使用
    assert fake_runtime.last_call is not None  # 验证 Runtime 被调用
    assert fake_runtime.last_call["messages"] == [  # 验证传入 Runtime 的消息已被策略处理
        {"role": "system", "content": "你是一个乐于助人的助手。"},
        {"role": "user", "content": "hi"},
    ]


@pytest.mark.asyncio  # 标记异步测试
async def test_chat_service_releases_lock_when_stream_is_closed_early(fake_redis, session_with_history):
    """测试当调用方提前关闭流时，锁作用域仍会释放锁。"""

    class BlockingAgentLoop:
        """用于测试提前关闭流的阻塞型 AgentLoop。"""

        def __init__(self) -> None:
            """初始化阻塞型 AgentLoop。"""
            self._continue_event = asyncio.Event()  # 使用事件控制后续流程是否继续

        async def run(self, *, run_id, profile, messages, session_id="", context=None):  # profile-only 签名：所有运行依赖通过 profile 注入
            """先产出 run_started，再等待外部关闭或继续。"""
            yield RunStartedEvent(run_id=run_id, session_id=session_id)  # 先产出开始事件，确保锁已持有
            await self._continue_event.wait()  # 阻塞等待，给测试留出提前关闭流的窗口
            yield RunCompletedEvent(run_id=run_id, output="done")  # 若未被关闭，则后续可正常完成

    lock_store = RedisLockStore(fake_redis, key_prefix="test")  # 创建测试用锁存储
    settings = Settings(  # 创建测试配置
        redis_url="redis://localhost:6379",
        session_lock_ttl_seconds=30,
    )
    service = ChatService(
        session_store=RedisSessionStore(fake_redis, key_prefix="test"),
        run_store=RedisRunStore(fake_redis, key_prefix="test"),
        lock_store=lock_store,
        agent_provider=FakeAgentProvider(),
        agent_loop=BlockingAgentLoop(),
        settings=settings,
        redis=fake_redis,  # 注入 Redis 客户端
    )

    stream = service.stream_chat("session-1", "hi")  # 获取异步事件流，但先不完整消费
    first_event = await anext(stream)  # 只消费第一个事件，确保已进入锁作用域
    assert first_event.event_name == "run_started"  # 验证流已正式开始

    active_run_id = await lock_store.get_active_run_id("session-1")  # 在流关闭前确认锁确实已持有
    assert active_run_id is not None  # 验证当前会话处于加锁状态

    await stream.aclose()  # 主动关闭异步生成器，模拟上游连接提前断开

    released_run_id = await lock_store.get_active_run_id("session-1")  # 关闭后再次读取锁 owner
    assert released_run_id is None  # 验证锁已随着生成器关闭而释放


@pytest.mark.asyncio
async def test_chat_service_fails_run_when_lock_heartbeat_is_lost(fake_redis, session_with_history):
    """测试心跳失锁后会把当前 run 收敛为失败态。"""

    class BlockingAgentLoop:
        """用于等待心跳丢锁的阻塞型 AgentLoop。"""

        async def run(self, *, run_id, profile, messages, session_id="", context=None):  # profile-only 签名：所有运行依赖通过 profile 注入
            """先产出开始事件，再阻塞等待后台取消。"""
            yield RunStartedEvent(run_id=run_id, session_id=session_id)  # 先让 run 进入执行中
            await asyncio.sleep(10)  # 阻塞等待，让后台心跳有机会触发失锁
            yield RunCompletedEvent(run_id=run_id, output="done")  # 正常情况下该分支不应执行

    lock_store = RedisLockStore(fake_redis, key_prefix="test")  # 创建测试用锁存储
    extend_calls = 0  # 记录续期调用次数

    async def failing_extend(session_id: str, run_id: str, ttl_seconds: int) -> bool:
        """首次心跳即返回失败，模拟当前 run 失锁。"""
        nonlocal extend_calls  # 使用外层计数器记录调用次数
        extend_calls += 1  # 记录本次续期调用
        return False  # 返回 False，表示失去锁 owner

    lock_store.extend = failing_extend  # 替换为失败续期函数
    settings = Settings(  # 创建测试配置
        redis_url="redis://localhost:6379",
        session_lock_ttl_seconds=2,
    )
    service = ChatService(
        session_store=RedisSessionStore(fake_redis, key_prefix="test"),
        run_store=RedisRunStore(fake_redis, key_prefix="test"),
        lock_store=lock_store,
        agent_provider=FakeAgentProvider(),
        agent_loop=BlockingAgentLoop(),
        settings=settings,
        redis=fake_redis,  # 注入 Redis 客户端
    )

    events = [event async for event in service.stream_chat("session-1", "hi")]  # 消费完整事件流

    assert [event.event_name for event in events] == ["run_started", "run_failed"]  # 验证最终收敛为失败
    assert events[-1].error_code == ErrorCode.SESSION_LOCK_HEARTBEAT_FAILED  # 验证错误码正确
    assert extend_calls >= 1  # 验证后台心跳确实执行过

    actual_run_id = events[0].run_id  # 获取真实 run_id，确认失败态已落库
    run_store = RedisRunStore(fake_redis, key_prefix="test")  # 创建 Run 存储
    persisted_run = await run_store.get_run(actual_run_id)  # 查询持久化的 Run
    assert persisted_run is not None  # 验证 Run 已存在
    assert persisted_run.status == RunStatus.FAILED  # 验证状态被收敛为失败
    assert persisted_run.error_code == ErrorCode.SESSION_LOCK_HEARTBEAT_FAILED  # 验证持久化错误码正确

    active_run_id = await lock_store.get_active_run_id("session-1")  # 再次确认会话锁已经释放
    assert active_run_id is None  # 验证作用域退出后锁已释放


@pytest.mark.asyncio
async def test_chat_service_marks_session_history_dirty_when_context_builder_detects_broken_tool_pairs(
    fake_redis,
):
    """测试检测到工具消息错乱时，会打脏标记并继续完成聊天。"""
    session_store = RedisSessionStore(fake_redis, key_prefix="test")
    session = Session(
        session_id="session-1",
        agent_id="default-agent",
        created_at=datetime.now(timezone.utc),
    )
    await session_store.create_session(session)
    await session_store.append_message(
        "session-1",
        StoredMessage.create(
            role="tool",
            content="孤儿工具结果",
            timestamp=datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc),
            tool_call_id="call-orphan",
            name="search",
        ),
    )

    fake_runtime = FakeAgentRuntime(
        events=[
            RunStartedEvent(run_id="run-1", session_id="session-1"),
            RunCompletedEvent(run_id="run-1", output="Done"),
        ]
    )
    agent_loop = AgentLoop(default_max_turns=10)  # 创建 Agent 循环（无状态设计）

    # 将 fake_runtime 注入到 profile，确保 AgentLoop 使用正确的模拟 Runtime
    agent = Agent(  # 创建默认 Agent 静态配置
        agent_id="master-agent",
        name="Master Agent",
        model="gpt-4.1-mini",
        system_prompt="你是一个乐于助人的助手。",
        temperature=0.2,
    )
    tool_registry = ToolRegistry()  # 创建工具注册表
    profile = AgentExecutionProfile(  # 构造执行 profile
        agent_id=agent.agent_id,
        agent=agent,
        prompt_source=AgentPromptSource(kind="file", path="master_prompt.md"),
        runtime=fake_runtime,  # 注入返回 RunCompleted 的模拟 Runtime
        tool_registry=tool_registry,
        tool_hook_pipeline=ToolHookPipeline(),
        max_turns=10,
    )
    service = ChatService(
        session_store=session_store,
        run_store=RedisRunStore(fake_redis, key_prefix="test"),
        lock_store=RedisLockStore(fake_redis, key_prefix="test"),
        agent_provider=FakeAgentProvider(profile=profile),  # 注入带自定义事件的 profile
        agent_loop=agent_loop,
        settings=Settings(
            redis_url="redis://localhost:6379",
            session_lock_ttl_seconds=30,
        ),
        redis=fake_redis,  # 注入 Redis 客户端
    )

    events = [event async for event in service.stream_chat("session-1", "继续")]  # 流式聊天

    assert events[-1].event_name == "run_completed"
    assert await session_store.is_history_dirty("session-1") is True
    assert fake_runtime.last_call is not None
    assert fake_runtime.last_call["messages"][:4] == [
        {"role": "system", "content": "你是一个乐于助人的助手。"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call-orphan",
                    "type": "function",
                    "function": {"name": "search", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "content": "孤儿工具结果",
            "tool_call_id": "call-orphan",
            "name": "search",
        },
        {"role": "user", "content": "继续"},
    ]


@pytest.mark.asyncio
async def test_chat_service_returns_context_compression_failed_when_trim_policy_raises(
    fake_redis,
    session_with_history,
):
    """测试上下文压缩失败时，会返回稳定的 CONTEXT_COMPRESSION_FAILED 错误。"""

    class FailingCompressionPolicy(ContextTrimPolicy):
        """始终抛出压缩失败的测试策略。"""

        async def build_messages(
            self,
            *,
            agent: Agent,
            system_message: StoredMessage,
            history: list[StoredMessage],
            history_indices: list[int] | None = None,
            current_user_message: StoredMessage | None,
            session_id: str | None = None,
            summary_target=None,
            extra_system_messages: list[str] | None = None,
        ) -> list[StoredMessage]:
            """直接抛出压缩异常，模拟摘要失败或超限。"""
            del agent, system_message, history, history_indices, current_user_message, session_id, summary_target, extra_system_messages
            raise ContextCompressionError("上下文压缩失败")

    fake_runtime = FakeAgentRuntime(events=[])
    service = ChatService(
        session_store=RedisSessionStore(fake_redis, key_prefix="test"),
        run_store=RedisRunStore(fake_redis, key_prefix="test"),
        lock_store=RedisLockStore(fake_redis, key_prefix="test"),
        agent_provider=FakeAgentProvider(),
        agent_loop=AgentLoop(default_max_turns=10),  # 创建 Agent 循环（无状态设计）
        settings=Settings(
            redis_url="redis://localhost:6379",
            session_lock_ttl_seconds=30,
        ),
        context_trim_policy=FailingCompressionPolicy(),
        redis=fake_redis,  # 注入 Redis 客户端
    )

    events = [event async for event in service.stream_chat(session_with_history.session_id, "hi")]

    assert len(events) == 1
    assert events[0].event_name == "request_failed"
    assert events[0].error_code == ErrorCode.CONTEXT_COMPRESSION_FAILED
    assert events[0].run_id is None


@pytest.mark.asyncio
async def test_chat_service_flushes_background_tool_write_before_terminal_and_lock_release(
    fake_redis,
    session_with_history,
):
    """测试中间 tool 消息可先外发，但终态前仍必须等后台写链 flush 完成。"""

    class ToolThenCompleteAgentLoop:
        """按固定顺序产出 run_started、tool_use_completed、run_completed 的测试循环。"""

        async def run(self, *, run_id, profile, messages, session_id="", context=None):  # profile-only 签名：所有运行依赖通过 profile 注入
            """产出最小事件序列，验证后台写链与终态 flush 关系。"""
            del profile, messages, context
            yield RunStartedEvent(run_id=run_id, session_id=session_id)
            yield ToolUseCompletedEvent(
                run_id=run_id,
                tool_name="search",
                tool_call_id="call-1",
                is_error=False,
                result="tool result",
            )
            yield RunCompletedEvent(run_id=run_id, output="done")

    session_store = RedisSessionStore(fake_redis, key_prefix="test")
    original_append = session_store.append_main_message
    tool_write_gate = asyncio.Event()  # 控制 tool 消息后台写入何时放行，验证终态会等待 flush
    call_order: list[str] = []

    async def controlled_append(
        session_id: str,
        message: StoredMessage,
        source_run_id: str | None = None,
        child_id: str | None = None,
    ) -> None:
        """让 tool 消息写入阻塞，模拟慢 Redis 写场景。"""
        del child_id
        if message.role == "tool":
            call_order.append("tool_write_started")
            await tool_write_gate.wait()
            call_order.append("tool_write_finished")
        await original_append(session_id, message, source_run_id=source_run_id)

    session_store.append_main_message = controlled_append
    lock_store = RedisLockStore(fake_redis, key_prefix="test")
    original_release = lock_store.release

    async def recording_release(session_id: str, run_id: str) -> bool:
        """记录锁释放时机，验证其位于后台写完成之后。"""
        call_order.append("release")
        return await original_release(session_id, run_id)

    lock_store.release = recording_release
    service = ChatService(
        session_store=session_store,
        run_store=RedisRunStore(fake_redis, key_prefix="test"),
        lock_store=lock_store,
        agent_provider=FakeAgentProvider(),
        agent_loop=ToolThenCompleteAgentLoop(),
        settings=Settings(
            redis_url="redis://localhost:6379",
            session_lock_ttl_seconds=30,
        ),
        redis=fake_redis,
    )

    try:
        stream = service.stream_chat(session_with_history.session_id, "hi")
        first_event = await anext(stream)
        second_event = await anext(stream)

        assert first_event.event_name == "run_started"
        assert second_event.event_name == "tool_use_completed"  # tool 事件应先对外发出，不被 Redis 写阻塞

        terminal_task = asyncio.create_task(anext(stream))  # 后续终态会等待后台写链 flush，因此这里先异步挂起
        await asyncio.sleep(0.05)  # 给后台写链和终态 flush 留出调度机会

        assert terminal_task.done() is False  # gate 未放行前，run_completed 不应提前发出
        assert await lock_store.get_active_run_id(session_with_history.session_id) is not None  # 终态未完成前锁仍应保持占有

        tool_write_gate.set()  # 放行后台 tool 写入，允许 flush 结束并继续终态流程
        terminal_event = await terminal_task

        assert terminal_event.event_name == "run_completed"
        assert await lock_store.get_active_run_id(session_with_history.session_id) is None
        assert call_order == ["tool_write_started", "tool_write_finished", "release"]  # 后台写必须先结束，锁才能释放
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_chat_service_converts_background_write_failure_to_run_failed(
    fake_redis,
    session_with_history,
):
    """测试后台写链 flush 失败时，会在锁内把当前 run 收敛为失败态。"""

    class ToolThenCompleteAgentLoop:
        """产出最小 tool -> completed 链路，触发后台写失败后的 flush。"""

        async def run(self, *, run_id, profile, messages, session_id="", context=None):  # profile-only 签名：所有运行依赖通过 profile 注入
            """发出一个 tool 完成事件，再立刻发出 run_completed。"""
            del profile, messages, context
            yield RunStartedEvent(run_id=run_id, session_id=session_id)
            yield ToolUseCompletedEvent(
                run_id=run_id,
                tool_name="search",
                tool_call_id="call-1",
                is_error=False,
                result="tool result",
            )
            yield RunCompletedEvent(run_id=run_id, output="done")

    session_store = RedisSessionStore(fake_redis, key_prefix="test")
    original_append = session_store.append_main_message

    async def failing_append(
        session_id: str,
        message: StoredMessage,
        source_run_id: str | None = None,
        child_id: str | None = None,
    ) -> None:
        """让 tool 消息写入失败，验证 ChatService 会把 run 收敛为失败态。"""
        del child_id
        if message.role == "tool":
            raise RuntimeError("tool append failed")
        await original_append(session_id, message, source_run_id=source_run_id)

    session_store.append_main_message = failing_append
    service = ChatService(
        session_store=session_store,
        run_store=RedisRunStore(fake_redis, key_prefix="test"),
        lock_store=RedisLockStore(fake_redis, key_prefix="test"),
        agent_provider=FakeAgentProvider(),
        agent_loop=ToolThenCompleteAgentLoop(),
        settings=Settings(
            redis_url="redis://localhost:6379",
            session_lock_ttl_seconds=30,
        ),
        redis=fake_redis,
    )

    try:
        events = [event async for event in service.stream_chat(session_with_history.session_id, "hi")]
    finally:
        await service.aclose()

    assert [event.event_name for event in events] == ["run_started", "tool_use_completed", "run_failed"]
    assert events[-1].error_code == ErrorCode.LLM_REQUEST_FAILED
    assert "tool append failed" in events[-1].message

    actual_run_id = events[0].run_id
    run_store = RedisRunStore(fake_redis, key_prefix="test")
    persisted_run = await run_store.get_run(actual_run_id)
    assert persisted_run is not None
    assert persisted_run.status == RunStatus.FAILED
    assert persisted_run.error_code == ErrorCode.LLM_REQUEST_FAILED

    messages = await session_store.list_messages(session_with_history.session_id)
    assert [message.role for message in messages] == ["user", "assistant", "user"]  # 仅保留原历史与本次用户消息，失败的 tool/assistant 终态都不应写入
