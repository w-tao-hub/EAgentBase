"""ChildAgentRunner 测试。

测试 child run 的创建、child 上下文的写入、resume 校验机制
以及子代理类型一致性检查等核心功能。
"""

from __future__ import annotations  # 启用未来注解，避免前向引用问题

from datetime import datetime, timezone  # 导入日期时间类，用于构造时间戳
import asyncio
from unittest.mock import MagicMock  # 导入 MagicMock，用于构造最小 runner 依赖

import pytest  # 导入 pytest 测试框架

from app.config import Settings  # 导入应用配置
from app.core.hooks import ToolHookPipeline  # 导入空 Hook 管线
from app.core.loop.agent_loop import AgentLoop  # 导入 AgentLoop 编排器
from app.core.models.error import ErrorCode  # 导入错误码，用于断言取消错误语义
from app.core.runtime.agent_runtime import TurnComplete  # 导入 TurnComplete 标记，用于模拟 stream_once 返回
from app.core.models.agent import Agent, AgentExecutionProfile, AgentPromptSource  # 导入 Agent 相关模型
from app.core.models.run import Run, RunStatus  # 导入 Run 模型和状态枚举
from app.core.models.session import Session  # 导入 Session 模型
from app.core.models.stored_message import StoredMessage  # 导入存储消息模型
from app.core.models.tool import Tool, ToolResult, ToolRegistry  # 导入工具基类、结果模型和注册表
from app.core.models.execution_context import ExecutionContext  # 导入执行上下文
from app.infra.store.redis_run_store import RedisRunStore  # 导入 Run 存储
from app.infra.store.redis_session_store import RedisSessionStore  # 导入 Session 存储
from app.services.child_agent_runner import ChildAgentRunner  # 导入被测试的 ChildAgentRunner
from tests.fakes import FakeAgentRuntime  # 导入假运行时


def _plan_profile(runtime: FakeAgentRuntime) -> AgentExecutionProfile:
    """构造 Plan 子代理的 AgentExecutionProfile。

    返回一个最小可用的 profile，用于测试场景中注册为 child profile。
    该 profile 使用 FakeAgentRuntime 替代真实 LLM 交互。

    Args:
        runtime: 假 AgentRuntime 实例，用于控制 child loop 行为

    Returns:
        AgentExecutionProfile: Plan 子代理的执行配置
    """
    agent = Agent(  # 构造 Plan 子代理的 Agent 静态配置
        agent_id="Plan",  # agent 唯一标识
        name="Plan",  # 展示名称
        model="gpt-4.1-mini",  # 模型名称
        system_prompt="你是计划代理。",  # 系统提示词
        temperature=0.2,  # 温度参数
    )
    return AgentExecutionProfile(  # 组装完整执行配置
        agent_id="Plan",  # profile 的 agent_id
        agent=agent,  # Agent 静态配置
        prompt_source=AgentPromptSource(kind="file", path="plan.md"),  # prompt 来源
        runtime=runtime,  # 传入假运行时
        tool_registry=ToolRegistry(),  # 空的工具注册表
        tool_hook_pipeline=ToolHookPipeline(),  # 空的 Hook 管线
        max_turns=3,  # 最大轮数限制
    )


@pytest.mark.asyncio  # 标记为异步测试
async def test_child_runner_persists_child_run_and_context(fake_redis) -> None:
    """测试 child runner 会创建 child run 并写入 child 上下文。

    验证核心流程：
    1. child run 被正确创建并持久化到数据库
    2. child 上下文消息被写入到隔离的 child context key
    3. 消息中携带正确的 subagent_type 元数据
    4. child run 的状态正确流转到 COMPLETED
    """
    session_store = RedisSessionStore(fake_redis, key_prefix="test")  # 创建会话存储
    run_store = RedisRunStore(fake_redis, key_prefix="test")  # 创建运行存储
    runtime = FakeAgentRuntime(events=[])  # 创建空事件的运行时（无额外事件输出）
    profile = _plan_profile(runtime)  # 构造 Plan profile

    runner = ChildAgentRunner(  # 创建被测试的 ChildAgentRunner
        session_store=session_store,  # 注入会话存储
        run_store=run_store,  # 注入运行存储
        redis=fake_redis,  # 注入 Redis 客户端
        agent_loop=AgentLoop(),  # 注入 AgentLoop
        child_profiles={"Plan": profile},  # 注册 Plan profile
        settings=Settings(redis_url="redis://localhost:6379/0"),  # 最小配置
    )

    result = await runner.run_child(  # 执行 child run
        session_id="session-1",  # 所属会话
        parent_run_id="master-run",  # 父 run ID
        tool_call_id="call-1",  # 触发工具调用 ID
        subagent_type="Plan",  # 子代理类型
        child_id="plan-abc",  # child 稳定标识
        prompt="请制定计划",  # 任务 prompt
        description="制定计划测试",  # 任务描述
        metadata=None,  # 无额外元数据
        cancel_event=None,  # 无取消事件
    )

    # 验证返回结果
    assert result.child_id == "plan-abc"  # child_id 正确返回
    assert result.output == ""  # 空事件运行时输出为空字符串

    # 验证 child run 被正确持久化
    child_run = await run_store.get_run(result.child_run_id)  # 读取 child run 记录
    assert child_run is not None  # run 记录存在
    assert child_run.status == RunStatus.COMPLETED  # 状态为已完成
    assert child_run.parent_run_id == "master-run"  # 父 run ID 正确
    assert child_run.child_id == "plan-abc"  # child_id 正确
    assert child_run.tool_call_id == "call-1"  # tool_call_id 正确

    # 验证 child 上下文消息
    messages = await session_store.list_child_messages("session-1", "plan-abc")  # 读取 child 上下文
    assert len(messages) > 0  # 至少有一条消息
    assert messages[0].role == "user"  # 首条消息是 user 角色
    assert messages[0].meta.subagent_type == "Plan"  # 消息携带正确的 subagent_type 元数据

    ttl = await fake_redis.ttl(f"test:run:{result.child_run_id}")  # 读取 child run key 的 TTL
    assert ttl > 0  # child run 也应具备 TTL
    assert ttl <= runner._settings.run_ttl_seconds  # TTL 应受配置约束


@pytest.mark.asyncio  # 标记为异步测试
async def test_child_runner_rejects_resume_with_different_subagent_type(fake_redis) -> None:
    """测试同一 child_id 不能切换 subagent_type。

    当 child 上下文中已有某个 subagent_type 的消息记录时，
    如果尝试以不同的 subagent_type 在该 child_id 上创建新 run，
    应该抛出 CHILD_AGENT_CONTEXT_INVALID 错误。
    """
    session_store = RedisSessionStore(fake_redis, key_prefix="test")  # 创建会话存储
    run_store = RedisRunStore(fake_redis, key_prefix="test")  # 创建运行存储

    # 预写入一条 Plan 类型的 child 消息，模拟该 child 已有历史上下文
    await session_store.append_child_message(  # 使用新的 subagent_type 参数
        session_id="session-1",  # 所属会话
        child_id="plan-abc",  # child 稳定标识
        message=StoredMessage.create(  # 创建一条用户消息
            role="user",  # 用户角色
            content="旧任务",  # 消息内容
            timestamp=datetime.now(timezone.utc),  # 当前时间戳
            subagent_type="Plan",  # 标记为 Plan 类型
        ),
        subagent_type="Plan",  # 写入时同步透传 subagent_type
    )

    runner = ChildAgentRunner(  # 创建 ChildAgentRunner，但只注册 "Other" profile
        session_store=session_store,  # 注入会话存储
        run_store=run_store,  # 注入运行存储
        redis=fake_redis,  # 注入 Redis 客户端
        agent_loop=AgentLoop(),  # 注入 AgentLoop
        child_profiles={"Other": _plan_profile(FakeAgentRuntime())},  # 只注册 Other，不含 Plan
        settings=Settings(redis_url="redis://localhost:6379/0"),  # 最小配置
    )

    # 尝试以 "Other" 类型在已有的 "plan-abc" child_id 上启动 run，应该被拒绝
    with pytest.raises(ValueError, match="CHILD_AGENT_CONTEXT_INVALID"):  # 断言抛出 ValueError
        await runner.run_child(  # 尝试执行 child run
            session_id="session-1",  # 所属会话
            parent_run_id="master-run",  # 父 run ID
            tool_call_id="call-1",  # 触发工具调用 ID
            subagent_type="Other",  # 与已有类型不一致
            child_id="plan-abc",  # 已有 Plan 类型的 child_id
            prompt="新任务",  # 任务 prompt
            description="测试",  # 任务描述
            metadata=None,  # 无额外元数据
            cancel_event=None,  # 无取消事件
        )


@pytest.mark.asyncio  # 标记为异步测试
async def test_child_runner_rejects_resume_to_nonexistent_child(fake_redis) -> None:
    """测试 resume 到不存在的 child_id 时返回 CHILD_AGENT_CONTEXT_INVALID。

    当 is_resume=True 但指定的 child_id 在 child 上下文中没有任何消息时，
    ChildAgentRunner 应该抛出 CHILD_AGENT_CONTEXT_INVALID 错误，
    不允许静默创建新的上下文。
    """
    session_store = RedisSessionStore(fake_redis, key_prefix="test")  # 创建会话存储
    run_store = RedisRunStore(fake_redis, key_prefix="test")  # 创建运行存储

    runner = ChildAgentRunner(  # 创建 ChildAgentRunner
        session_store=session_store,  # 注入会话存储
        run_store=run_store,  # 注入运行存储
        redis=fake_redis,  # 注入 Redis 客户端
        agent_loop=AgentLoop(),  # 注入 AgentLoop
        child_profiles={"Plan": _plan_profile(FakeAgentRuntime())},  # 注册 Plan profile
        settings=Settings(redis_url="redis://localhost:6379/0"),  # 最小配置
    )

    # 尝试 resume 到一个从未存在过的 child_id，应该被拒绝
    with pytest.raises(ValueError, match="CHILD_AGENT_CONTEXT_INVALID"):  # 断言抛出 ValueError
        await runner.run_child(  # 尝试执行 child run
            session_id="session-1",  # 所属会话
            parent_run_id="master-run",  # 父 run ID
            tool_call_id="call-1",  # 触发工具调用 ID
            subagent_type="Plan",  # 子代理类型
            child_id="plan-nonexistent",  # 不存在的 child_id
            prompt="新任务",  # 任务 prompt
            description="测试",  # 任务描述
            metadata=None,  # 无额外元数据
            cancel_event=None,  # 无取消事件
            is_resume=True,  # 标记为 resume 模式
        )


@pytest.mark.asyncio  # 标记为异步测试
async def test_child_runner_marks_history_dirty(fake_redis) -> None:
    """测试 child 上下文检测到历史脏数据时会调用 mark_child_history_dirty。

    验证当上下文构建器在归一化过程中检测到工具消息配对错乱时，
    ChildAgentRunner 会正确标记 child 历史为 dirty 状态，
    同时 child run 仍正常完成执行流程。
    """
    from app.core.runtime.agent_runtime import TurnComplete  # 导入 TurnComplete 标记

    session_store = RedisSessionStore(fake_redis, key_prefix="test")  # 创建会话存储
    run_store = RedisRunStore(fake_redis, key_prefix="test")  # 创建运行存储
    # 使用 turn_results 模式模拟 AgentLoop 的正常执行流
    runtime = FakeAgentRuntime(events=[])  # 空事件列表，由 turn_results 接管行为
    runtime.turn_results = [["child output", TurnComplete()]]  # 预设单轮返回：文本片段 + 完成标记
    profile = _plan_profile(runtime)  # 构造 Plan profile
    runner = ChildAgentRunner(  # 创建被测试的 ChildAgentRunner
        session_store=session_store,  # 注入会话存储
        run_store=run_store,  # 注入运行存储
        redis=fake_redis,  # 注入 Redis 客户端
        agent_loop=AgentLoop(),  # 注入 AgentLoop
        child_profiles={"Plan": profile},  # 注册 Plan profile
        settings=Settings(redis_url="redis://localhost:6379/0"),  # 最小配置
    )

    result = await runner.run_child(  # 执行 child run
        session_id="session-1",  # 所属会话
        parent_run_id="master-run",  # 父 run ID
        tool_call_id="call-1",  # 触发工具调用 ID
        subagent_type="Plan",  # 子代理类型
        child_id="plan-abc",  # child 稳定标识
        prompt="请制定计划",  # 任务 prompt
        description="制定计划测试",  # 任务描述
        metadata=None,  # 无额外元数据
        cancel_event=None,  # 无取消事件
    )

    assert result.child_id == "plan-abc"  # child_id 正确返回
    # 验证 child run 创建并完成
    child_run = await run_store.get_run(result.child_run_id)  # 读取 child run 记录
    assert child_run is not None  # run 记录存在
    assert child_run.status == RunStatus.COMPLETED  # 状态为已完成
    assert await session_store.is_child_history_dirty("session-1", "plan-abc") is False  # 正常历史不应误标 dirty


@pytest.mark.asyncio
async def test_child_runner_marks_history_dirty_when_context_contains_orphan_tool(fake_redis) -> None:
    """测试 child 历史存在孤儿 tool 消息时会真实写入 dirty 标记。"""
    session_store = RedisSessionStore(fake_redis, key_prefix="test")
    run_store = RedisRunStore(fake_redis, key_prefix="test")
    runtime = FakeAgentRuntime(turn_results=[["child output", TurnComplete()]])
    profile = _plan_profile(runtime)
    runner = ChildAgentRunner(
        session_store=session_store,
        run_store=run_store,
        redis=fake_redis,
        agent_loop=AgentLoop(),
        child_profiles={"Plan": profile},
        settings=Settings(redis_url="redis://localhost:6379/0"),
    )

    await session_store.append_child_message(
        session_id="session-1",
        child_id="plan-abc",
        message=StoredMessage.create(
            role="tool",
            content="孤儿工具结果",
            tool_call_id=None,
            name="search",
            timestamp=datetime.now(timezone.utc),
            subagent_type="Plan",
        ),
        subagent_type="Plan",
    )

    await runner.run_child(
        session_id="session-1",
        parent_run_id="master-run",
        tool_call_id="call-1",
        subagent_type="Plan",
        child_id="plan-abc",
        prompt="请制定计划",
        description="测试",
        metadata=None,
        cancel_event=None,
        is_resume=True,
    )

    assert await session_store.is_child_history_dirty("session-1", "plan-abc") is True


@pytest.mark.asyncio  # 标记为异步测试
async def test_child_runner_handles_run_failed_event(fake_redis) -> None:
    """测试 _consume_child_loop 处理 RunFailedEvent：run 进入 FAILED 并抛出异常。

    当 LLM 调用失败时，AgentLoop 会产生 RunFailedEvent。
    _consume_child_loop 应更新 child run 为 FAILED 状态，
    并抛出包含 CHILD_AGENT_EXECUTION_FAILED 错误码的 ValueError。
    """
    session_store = RedisSessionStore(fake_redis, key_prefix="test")  # 创建会话存储
    run_store = RedisRunStore(fake_redis, key_prefix="test")  # 创建运行存储
    child_agent = Agent(  # 构造 Plan 子代理的 Agent 静态配置
        agent_id="Plan",  # agent 唯一标识
        name="Plan",  # 展示名称
        model="gpt-4.1-mini",  # 模型名称
        system_prompt="你是计划代理。",  # 系统提示词
        temperature=0.2,  # 温度参数
    )
    # 使用 raise_error=True 的 FakeAgentRuntime 模拟 LLM 调用抛出异常
    # 异常会被 AgentLoop 捕获并生成 RunFailedEvent
    runtime = FakeAgentRuntime(raise_error=True)  # raise_error=True 使 stream_once 抛出异常
    profile = AgentExecutionProfile(  # 手动构造完整 profile
        agent_id="Plan",  # profile 的 agent_id
        agent=child_agent,  # Agent 静态配置
        prompt_source=AgentPromptSource(kind="file", path="plan.md"),  # prompt 来源
        runtime=runtime,  # 传入会抛异常的假运行时
        tool_registry=ToolRegistry(),  # 空的工具注册表
        tool_hook_pipeline=ToolHookPipeline(),  # 空的 Hook 管线
        max_turns=3,  # 最大轮数限制
    )
    runner = ChildAgentRunner(  # 创建被测试的 ChildAgentRunner
        session_store=session_store,  # 注入会话存储
        run_store=run_store,  # 注入运行存储
        redis=fake_redis,  # 注入 Redis 客户端
        agent_loop=AgentLoop(),  # 注入 AgentLoop
        child_profiles={"Plan": profile},  # 注册 Plan profile
        settings=Settings(redis_url="redis://localhost:6379/0"),  # 最小配置
    )

    with pytest.raises(ValueError, match="CHILD_AGENT_EXECUTION_FAILED"):  # 断言抛出包含正确错误码的异常
        await runner.run_child(  # 尝试执行 child run
            session_id="session-1",  # 所属会话
            parent_run_id="master-run",  # 父 run ID
            tool_call_id="call-1",  # 触发工具调用 ID
            subagent_type="Plan",  # 子代理类型
            child_id="plan-fail",  # child 稳定标识
            prompt="请制定计划",  # 任务 prompt
            description="测试",  # 任务描述
            metadata=None,  # 无额外元数据
            cancel_event=None,  # 无取消事件
        )


@pytest.mark.asyncio  # 标记为异步测试
async def test_child_runner_handles_run_cancelled_event(fake_redis) -> None:
    """测试 child 收到取消信号时会落 CANCELLED 并抛出稳定错误。"""
    session_store = RedisSessionStore(fake_redis, key_prefix="test")
    run_store = RedisRunStore(fake_redis, key_prefix="test")
    runtime = FakeAgentRuntime(turn_results=[["partial", TurnComplete()]])
    profile = _plan_profile(runtime)
    runner = ChildAgentRunner(
        session_store=session_store,
        run_store=run_store,
        redis=fake_redis,
        agent_loop=AgentLoop(),
        child_profiles={"Plan": profile},
        settings=Settings(redis_url="redis://localhost:6379/0"),
    )
    cancel_event = asyncio.Event()
    cancel_event.set()

    with pytest.raises(ValueError, match=ErrorCode.CHILD_AGENT_EXECUTION_FAILED.value):
        await runner.run_child(
            session_id="session-1",
            parent_run_id="master-run",
            tool_call_id="call-1",
            subagent_type="Plan",
            child_id="plan-cancelled",
            prompt="请制定计划",
            description="测试",
            metadata=None,
            cancel_event=cancel_event,
        )

    run_ids = await session_store.list_session_run_ids("session-1")
    child_runs = [await run_store.get_run(run_id) for run_id in run_ids]
    cancelled_run = next(run for run in child_runs if run is not None)
    assert cancelled_run.status == RunStatus.CANCELLED
    assert cancelled_run.error_code == ErrorCode.RUN_CANCELLED


@pytest.mark.asyncio
async def test_child_runner_accepts_valid_resume_and_appends_same_context(fake_redis) -> None:
    """测试同一 child_id 的合法 resume 会复用同一条长期上下文并继续追加消息。"""
    session_store = RedisSessionStore(fake_redis, key_prefix="test")
    run_store = RedisRunStore(fake_redis, key_prefix="test")
    runtime = FakeAgentRuntime(turn_results=[["第一次输出", TurnComplete()], ["第二次输出", TurnComplete()]])
    profile = _plan_profile(runtime)
    runner = ChildAgentRunner(
        session_store=session_store,
        run_store=run_store,
        redis=fake_redis,
        agent_loop=AgentLoop(),
        child_profiles={"Plan": profile},
        settings=Settings(redis_url="redis://localhost:6379/0"),
    )

    first = await runner.run_child(
        session_id="session-1",
        parent_run_id="master-run-1",
        tool_call_id="call-1",
        subagent_type="Plan",
        child_id="plan-resume",
        prompt="第一次任务",
        description="测试描述",
        metadata=None,
        cancel_event=None,
    )
    second = await runner.run_child(
        session_id="session-1",
        parent_run_id="master-run-2",
        tool_call_id="call-2",
        subagent_type="Plan",
        child_id="plan-resume",
        prompt="第二次任务",
        description="测试描述",
        metadata=None,
        cancel_event=None,
        is_resume=True,
    )

    assert first.child_id == second.child_id == "plan-resume"
    assert await session_store.list_session_children("session-1") == ["plan-resume"]
    messages = await session_store.list_child_messages("session-1", "plan-resume")
    assert [message.content for message in messages if message.role == "user"] == ["第一次任务", "第二次任务"]
    assert any(message.content == "第一次输出" for message in messages if message.role == "assistant")
    assert any(message.content == "第二次输出" for message in messages if message.role == "assistant")


@pytest.mark.asyncio
async def test_child_agent_runner_writes_summary_and_updates_description_on_resume(fake_redis) -> None:
    """测试首次派发写摘要，resume 只覆盖 description 不新增记录。"""
    session_store = RedisSessionStore(fake_redis, key_prefix="test")
    run_store = RedisRunStore(fake_redis, key_prefix="test")
    runtime = FakeAgentRuntime(turn_results=[["第一次输出", TurnComplete()], ["第二次输出", TurnComplete()]])
    profile = _plan_profile(runtime)
    runner = ChildAgentRunner(
        session_store=session_store,
        run_store=run_store,
        redis=fake_redis,
        agent_loop=AgentLoop(),
        child_profiles={"Plan": profile},
        settings=Settings(redis_url="redis://localhost:6379/0"),
    )

    await runner.run_child(
        session_id="session-1",
        parent_run_id="master-run-1",
        tool_call_id="call-1",
        subagent_type="Plan",
        child_id="plan-resume",
        prompt="第一次任务",
        description="第一次描述",
        metadata=None,
        cancel_event=None,
    )
    await runner.run_child(
        session_id="session-1",
        parent_run_id="master-run-2",
        tool_call_id="call-2",
        subagent_type="Plan",
        child_id="plan-resume",
        prompt="第二次任务",
        description="第二次描述",
        metadata=None,
        cancel_event=None,
        is_resume=True,
    )

    summaries = await session_store.list_session_child_summaries("session-1")

    assert len(summaries) == 1
    assert summaries[0].resume_id == "plan-resume"
    assert summaries[0].subagent_type == "Plan"
    assert summaries[0].description == "第二次描述"


@pytest.mark.asyncio
async def test_build_dynamic_profile_filters_child_filtered_tools():
    """验证 _build_dynamic_profile 会过滤 Task 和 ListResumableSubagents。"""
    tool_catalog: dict[str, Tool] = {"search": _create_stub_tool("search")}
    profile = _plan_profile(FakeAgentRuntime())  # 使用现有的 profile 构造辅助函数

    runner = ChildAgentRunner(
        session_store=MagicMock(),
        run_store=MagicMock(),
        redis=MagicMock(),
        agent_loop=MagicMock(),
        child_profiles={"Plan": profile},
        settings=MagicMock(),
        tool_catalog=tool_catalog,
    )

    result = runner._build_dynamic_profile(
        profile,
        ("search", "Task", "ListResumableSubagents"),
    )

    tool_names = result.tool_registry.list_tools()
    assert "search" in tool_names  # 非过滤工具应保留
    assert "Task" not in tool_names  # Task 被过滤
    assert "ListResumableSubagents" not in tool_names  # ListResumableSubagents 被过滤


@pytest.mark.asyncio
async def test_child_runner_failed_run_summary_still_written(fake_redis) -> None:
    """验证 child run 失败后摘要仍然存在（Issue 1 修复：不误删有效摘要）。

    当 child run 失败（RunFailedEvent）时，首条 user message 已在 append_child_message
    落库，因此 upsert_session_child_summary 应该写入。失败不应导致摘要被删除。
    """
    session_store = RedisSessionStore(fake_redis, key_prefix="test")
    run_store = RedisRunStore(fake_redis, key_prefix="test")
    await session_store.create_session(
        Session(session_id="session-fail", agent_id="master-agent", created_at=datetime.now(timezone.utc))
    )

    # raise_error=True 使 AgentLoop 抛出异常，触发 RunFailedEvent
    runtime = FakeAgentRuntime(raise_error=True)
    profile = _plan_profile(runtime)
    runner = ChildAgentRunner(
        session_store=session_store,
        run_store=run_store,
        redis=fake_redis,
        agent_loop=AgentLoop(),
        child_profiles={"Plan": profile},
        settings=Settings(redis_url="redis://localhost:6379/0"),
    )

    with pytest.raises(ValueError, match="CHILD_AGENT_EXECUTION_FAILED"):
        await runner.run_child(
            session_id="session-fail",
            parent_run_id="master-run",
            tool_call_id="call-1",
            subagent_type="Plan",
            child_id="plan-fail",
            prompt="分析任务",
            description="失败场景摘要",
            metadata=None,
            cancel_event=None,
        )

    # 即使 run 失败，摘要也应在（因为 message 已落库）
    summaries = await session_store.list_session_child_summaries("session-fail")
    assert len(summaries) == 1
    assert summaries[0].description == "失败场景摘要"


@pytest.mark.asyncio
async def test_child_runner_resume_wrong_type_preserves_old_summary(fake_redis) -> None:
    """验证 resume 时 subagent_type 不匹配，已有摘要不会被删除或覆盖。

    Issue 1 修复将摘要写入后置到 append_child_message 之后，且不包含异常回滚。
    此测试保证 resume 校验失败时旧摘要不受影响。
    """
    session_store = RedisSessionStore(fake_redis, key_prefix="test")
    run_store = RedisRunStore(fake_redis, key_prefix="test")
    await session_store.create_session(
        Session(session_id="session-type", agent_id="master-agent", created_at=datetime.now(timezone.utc))
    )
    runtime = FakeAgentRuntime(turn_results=[["输出", TurnComplete()]])
    runner = ChildAgentRunner(
        session_store=session_store,
        run_store=run_store,
        redis=fake_redis,
        agent_loop=AgentLoop(),
        child_profiles={"Plan": _plan_profile(runtime), "Worker": _plan_profile(FakeAgentRuntime())},
        settings=Settings(redis_url="redis://localhost:6379/0"),
    )

    # 首次派发，写入摘要
    await runner.run_child(
        session_id="session-type",
        parent_run_id="master-run-1",
        tool_call_id="call-1",
        subagent_type="Plan",
        child_id="plan-abc",
        prompt="第一次",
        description="Plan 摘要",
        metadata=None,
        cancel_event=None,
    )

    # 尝试以 Worker 类型 resume Plan child，应该失败
    with pytest.raises(ValueError, match="CHILD_AGENT_CONTEXT_INVALID"):
        await runner.run_child(
            session_id="session-type",
            parent_run_id="master-run-2",
            tool_call_id="call-2",
            subagent_type="Worker",
            child_id="plan-abc",
            prompt="第二次",
            description="Worker 摘要",
            metadata=None,
            cancel_event=None,
            is_resume=True,
        )

    # 旧摘要应保持不变
    summaries = await session_store.list_session_child_summaries("session-type")
    assert len(summaries) == 1
    assert summaries[0].subagent_type == "Plan"
    assert summaries[0].description == "Plan 摘要"


def _create_stub_tool(name: str) -> Tool:
    """创建测试用桩工具。"""
    class StubTool(Tool):
        @property
        def name(self) -> str:
            return name
        @property
        def description(self) -> str:
            return ""
        @property
        def input_schema(self) -> dict:
            return {"type": "object", "properties": {}}
        async def call(self, input: dict, context) -> ToolResult:
            return ToolResult(content="ok")
    return StubTool()
