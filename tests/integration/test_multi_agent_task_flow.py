"""多智能体 Task 派发集成测试。

验证 master 通过 Task 工具派发 Plan 子代理的完整流程：
- child 上下文与主上下文隔离（child 消息在 child_context_messages key 中）
- session_children 索引正确
- child run 正确创建（run_type=child, parent_run_id, child_id, tool_call_id）
- master 主上下文收到 tool 结果（tool_calls 的 tool_call_id 能对应上）
- 不同 child 上下文不互相污染
"""

from __future__ import annotations  # 启用未来注解

import json  # 导入 JSON 模块，用于序列化工具调用参数
from datetime import datetime, timezone  # 导入日期时间类，用于构造 Session 和消息时间戳

import pytest  # 导入 pytest 测试框架

from app.config import Settings  # 导入应用配置类
from app.core.hooks import ToolHookPipeline  # 导入工具 Hook 管线（空链）
from app.core.loop.agent_loop import AgentLoop  # 导入 AgentLoop 编排器
from app.core.models.agent import Agent, AgentExecutionProfile, AgentPromptSource  # 导入 Agent 相关模型
from app.core.models.execution_context import ExecutionContext  # 导入执行上下文模型
from app.core.models.run import Run, RunStatus, RunType  # 导入 Run 模型和状态枚举
from app.core.models.session import Session  # 导入 Session 模型
from app.core.models.tool import ToolRegistry  # 导入工具注册表
from app.core.runtime.agent_runtime import Function, ToolCall, TurnComplete  # 导入运行时类型
from app.infra.store.redis_run_store import RedisRunStore  # 导入 Redis Run 存储
from app.infra.store.redis_session_store import RedisSessionStore, SessionChildSummary  # 导入 Session 存储和 child 摘要数据类
from app.infra.tools.list_resumable_subagents_tool import ListResumableSubagentsTool  # 导入可恢复子代理查询工具
from app.infra.tools.task_tool import TaskTool  # 导入 Task 工具
from app.services.chat_event_processor import ChatEventProcessor  # 导入聊天事件分发器
from app.services.child_agent_runner import ChildAgentRunner  # 导入子代理执行服务
from tests.fakes import FakeAgentRuntime  # 导入假运行时


@pytest.mark.asyncio  # 标记为异步测试
async def test_task_dispatch_writes_child_context_and_master_tool_result(fake_redis):
    """测试 master 通过 Task 派发 Plan 后，child 上下文与主上下文隔离。

    验证点：
    1. session_children 索引包含 child_id
    2. child 消息在独立的 child_context_messages key 中
    3. child 消息的 meta.child_id 正确标记
    4. master 主上下文收到 tool 结果（role=tool）
    5. child run 正确创建（run_type=child, parent_run_id, child_id, tool_call_id）
    """
    session_store = RedisSessionStore(fake_redis, key_prefix="test")  # 创建会话存储实例
    run_store = RedisRunStore(fake_redis, key_prefix="test")  # 创建运行存储实例
    await session_store.create_session(  # 在 Redis 中创建测试会话
        Session(
            session_id="session-1",  # 会话唯一标识
            agent_id="master-agent",  # 主代理 ID
            created_at=datetime.now(timezone.utc),  # 当前 UTC 时间
        )
    )

    # ========== 构建子代理（Plan）的 profile 和 runner ==========

    # 子代理使用 FakeAgentRuntime：直接返回纯文本结果，不调用工具
    child_runtime = FakeAgentRuntime(
        turn_results=[
            ["子代理制定的实现计划。", TurnComplete()],  # 子代理第一轮（也是最后一轮）返回的文本和完成标记
        ]
    )
    child_agent = Agent(  # 子代理静态配置
        agent_id="Plan",  # 代理 ID
        name="Plan",  # 代理名称
        model="gpt-4.1-mini",  # 模型名称
        system_prompt="你是计划代理。",  # 系统提示词
        temperature=0.2,  # 温度参数
    )
    child_profile = AgentExecutionProfile(  # 子代理执行配置
        agent_id="Plan",  # profile 关联的 agent_id
        agent=child_agent,  # 子代理静态配置
        prompt_source=AgentPromptSource(kind="file", path="plan.md"),  # prompt 来源（测试中不实际读取文件）
        runtime=child_runtime,  # 假运行时
        tool_registry=ToolRegistry(),  # 子代理无工具可用
        tool_hook_pipeline=ToolHookPipeline(),  # 空 Hook 管线
        max_turns=3,  # 最大轮数限制
    )
    agent_loop = AgentLoop()  # 创建 AgentLoop 编排器实例
    child_runner = ChildAgentRunner(  # 创建子代理执行服务
        session_store=session_store,  # 会话存储
        run_store=run_store,  # 运行存储
        redis=fake_redis,  # Redis 客户端
        agent_loop=agent_loop,  # AgentLoop 编排器
        child_profiles={"Plan": child_profile},  # 注册的子代理 profile 映射
        settings=Settings(redis_url="redis://localhost:6379/0"),  # 应用配置（仅需 redis_url）
    )

    # ========== 构建主代理（master）的 profile ==========

    task_tool = TaskTool(child_runner)  # 创建 Task 工具，注入子代理执行服务
    master_tools = ToolRegistry()  # 创建主代理工具注册表
    master_tools.register(task_tool)  # 注册 Task 工具

    # 主代理使用 FakeAgentRuntime：第一轮返回 Task 工具调用，第二轮返回最终文本
    master_runtime = FakeAgentRuntime(
        turn_results=[  # 每轮 stream_once 将按索引消费
            [
                TurnComplete(  # 第一轮：只返回工具调用，无文本内容
                    tool_calls=[
                        ToolCall(  # 构造一个 Task 工具调用
                            id="call-task",  # 工具调用 ID
                            type="function",  # 类型固定为 function
                            function=Function(  # 函数调用信息
                                name="Task",  # 工具名称
                                arguments=json.dumps(  # 参数 JSON 字符串
                                    {
                                        "description": "制定计划",  # 简短任务描述
                                        "prompt": "请制定实现计划",  # 任务 prompt
                                        "subagent_type": "Plan",  # 子代理类型（大小写敏感）
                                    },
                                    ensure_ascii=False,  # 保留中文字符
                                ),
                            ),
                        )
                    ]
                )
            ],
            ["主代理收到子代理结果后的最终输出。", TurnComplete()],  # 第二轮：纯文本 + 无工具调用的完成标记
        ]
    )
    master_agent = Agent(  # 主代理静态配置
        agent_id="master-agent",  # 代理 ID
        name="Master Agent",  # 代理名称
        model="gpt-4.1-mini",  # 模型名称
        system_prompt="你是主代理。",  # 系统提示词
        temperature=0.2,  # 温度参数
    )
    master_profile = AgentExecutionProfile(  # 主代理执行配置
        agent_id="master-agent",  # profile 关联的 agent_id
        agent=master_agent,  # 主代理静态配置
        prompt_source=AgentPromptSource(kind="file", path="master_prompt.md"),  # prompt 来源
        runtime=master_runtime,  # 假运行时（控制多轮行为）
        tool_registry=master_tools,  # 包含 TaskTool 的工具注册表
        tool_hook_pipeline=ToolHookPipeline(),  # 空 Hook 管线
        max_turns=5,  # 最大轮数限制
    )

    # ========== 执行 master AgentLoop 并通过 ChatEventProcessor 处理事件 ==========

    processor = ChatEventProcessor(session_store)  # 创建聊天事件分发器
    pending_buffer = processor.create_pending_write_buffer(run_id="master-run")  # 创建后台写缓冲器
    context = ExecutionContext(  # 构造 master 执行上下文
        run_id="master-run",  # 主 run ID
        session_id="session-1",  # 会话 ID
        metadata=None,  # 无请求元数据
        agent=master_agent,  # 主代理配置
        run_type="master",  # master 类型
    )
    await run_store.create_run(
        Run(
            run_id="master-run",
            session_id="session-1",
            agent_id="master-agent",
            run_type="master",
            execution_mode="foreground",
            status=RunStatus.RUNNING,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    )
    await session_store.add_session_run("session-1", "master-run")

    # 执行 master AgentLoop，将事件交给 ChatEventProcessor 处理
    async for event in agent_loop.run(  # 遍历 AgentLoop 产生的事件
        run_id="master-run",  # 主 run ID
        profile=master_profile,  # 主代理执行配置
        messages=[{"role": "user", "content": "请派发 Plan"}],  # 初始用户消息
        session_id="session-1",  # 会话 ID
        context=context,  # 执行上下文
    ):
        await processor.process_event(  # 处理每个事件（区分转发与落库）
            session_id="session-1",  # 会话 ID
            event=event,  # 当前事件
            pending_write_buffer=pending_buffer,  # 后台写缓冲器
        )
    await pending_buffer.flush()  # 等待所有后台写入完成

    # ========== 验证 session_children 索引包含 child_id ==========

    child_ids = await session_store.list_session_children("session-1")  # 读取 session 下所有 child_id
    assert len(child_ids) > 0, "session_children 应至少包含一个 child_id"  # 至少有一个 child

    first_child_id = child_ids[0]  # 取第一个 child_id 用于后续验证

    # ========== 验证 child 消息隔离（在独立的 child_context_messages key 中） ==========

    child_messages = await session_store.list_child_messages("session-1", first_child_id)  # 读取 child 上下文消息
    assert len(child_messages) >= 2, f"child 上下文应至少包含 user 和 assistant 消息，实际 {len(child_messages)} 条"  # user + assistant

    # child 消息中应有 user 角色（child 的任务 prompt）
    assert any(message.role == "user" for message in child_messages), "child 消息中应包含 user 消息"

    # child 消息中应有 assistant 角色（child 的执行结果）
    assert any(message.role == "assistant" for message in child_messages), "child 消息中应包含 assistant 消息"

    # 所有 child 消息的 meta.child_id 应一致
    assert all(
        message.meta.child_id == first_child_id for message in child_messages
    ), "所有 child 消息的 meta.child_id 应与 child_id 一致"

    # ========== 验证 master 主上下文收到 tool 结果 ==========

    main_messages = await session_store.list_main_messages("session-1")  # 读取主会话上下文消息
    # 主上下文应包含 ChatEventProcessor 写入的 assistant 消息（带 tool_calls）和 tool 消息（Task 结果）
    tool_messages = [m for m in main_messages if m.role == "tool"]  # 筛选 role=tool 的消息
    assert len(tool_messages) > 0, "主上下文应包含 Task 工具结果消息"  # Task 工具结果在主上下文中

    # tool 消息的 tool_call_id 应与 master runtime 中定义的 ToolCall id 一致
    tool_msg = tool_messages[0]  # 取第一条 tool 消息
    assert tool_msg.tool_call_id == "call-task", f"tool 消息的 tool_call_id 应为 call-task，实际 {tool_msg.tool_call_id}"
    assert tool_msg.meta.child_id == first_child_id, f"tool 消息的 child_id 应为 {first_child_id}，实际 {tool_msg.meta.child_id}"

    # ========== 验证 child run 正确创建 ==========

    run_ids = await session_store.list_session_run_ids("session-1")  # 读取 session 关联的所有 run ID
    child_runs = []  # 收集 child 类型的 Run
    master_runs = []  # 收集 master 类型的 Run
    for rid in run_ids:  # 遍历所有 run ID
        run = await run_store.get_run(rid)  # 从 Redis 读取 Run 实例
        if run is not None and run.run_type == RunType.CHILD:  # 筛选 child 类型
            child_runs.append(run)  # 加入列表
        if run is not None and run.run_type == RunType.MASTER:
            master_runs.append(run)

    assert "master-run" in run_ids
    assert len(master_runs) == 1
    assert len(child_runs) >= 1, "应至少创建了一个 child run"  # 至少有一个 child run

    child_run = child_runs[0]  # 取第一个 child run 进行字段验证
    # child run 的 parent_run_id 应为 master-run（由 ChildAgentRunner.run_child 传入）
    assert child_run.parent_run_id == "master-run", (
        f"child run 的 parent_run_id 应为 master-run，实际 {child_run.parent_run_id}"
    )
    # child run 的 child_id 应与 session_children 中的 child_id 一致
    assert child_run.child_id == first_child_id, (
        f"child run 的 child_id 应为 {first_child_id}，实际 {child_run.child_id}"
    )
    # child run 的 tool_call_id 应与 master runtime 中定义的 ToolCall id 一致
    assert child_run.tool_call_id == "call-task", (
        f"child run 的 tool_call_id 应为 call-task，实际 {child_run.tool_call_id}"
    )
    # child run 的 agent_id 应为子代理的 agent_id（即 "Plan"）
    assert child_run.agent_id == "Plan", f"child run 的 agent_id 应为 Plan，实际 {child_run.agent_id}"


@pytest.mark.asyncio  # 标记为异步测试
async def test_different_child_contexts_are_isolated(fake_redis):
    """测试不同 child 的上下文互相隔离、不污染。

    场景：
    1. 派发 Plan 子代理 A，验证其上下文
    2. 派发 Plan 子代理 B，验证其上下文与 A 隔离
    3. 确认两个 child 的 child_context_messages 中的内容互不干扰
    """
    session_store = RedisSessionStore(fake_redis, key_prefix="test")  # 创建会话存储实例
    run_store = RedisRunStore(fake_redis, key_prefix="test")  # 创建运行存储实例
    await session_store.create_session(  # 创建测试会话
        Session(
            session_id="session-iso",  # 会话唯一标识
            agent_id="master-agent",  # 主代理 ID
            created_at=datetime.now(timezone.utc),  # 当前 UTC 时间
        )
    )

    # 子代理（Plan）的 profile，供两个 child 复用
    child_agent = Agent(  # 子代理静态配置
        agent_id="Plan",  # 代理 ID
        name="Plan",  # 代理名称
        model="gpt-4.1-mini",  # 模型名称
        system_prompt="你是计划代理。",  # 系统提示词
        temperature=0.2,  # 温度参数
    )
    agent_loop = AgentLoop()  # 创建 AgentLoop 编排器
    child_settings = Settings(redis_url="redis://localhost:6379/0")  # 应用配置

    # 第一次执行：派发 Plan 处理"需求 A"
    runtime_a = FakeAgentRuntime(  # 子代理 A 的假运行时
        turn_results=[
            ["针对需求 A 的计划。", TurnComplete()],  # 返回针对需求 A 的计划文本
        ]
    )
    profile_a = AgentExecutionProfile(  # 子代理 A 的执行配置
        agent_id="Plan",  # profile 关联的 agent_id
        agent=child_agent,  # 子代理静态配置
        prompt_source=AgentPromptSource(kind="file", path="plan.md"),  # prompt 来源
        runtime=runtime_a,  # 假运行时
        tool_registry=ToolRegistry(),  # 空工具注册表
        tool_hook_pipeline=ToolHookPipeline(),  # 空 Hook 管线
        max_turns=3,  # 最大轮数
    )
    runner_a = ChildAgentRunner(  # 子代理 A 的执行服务
        session_store=session_store,  # 会话存储
        run_store=run_store,  # 运行存储
        redis=fake_redis,  # Redis 客户端
        agent_loop=agent_loop,  # AgentLoop 编排器
        child_profiles={"Plan": profile_a},  # 注册子代理 profile
        settings=child_settings,  # 应用配置
    )
    # 直接调用 run_child 执行子代理 A
    result_a = await runner_a.run_child(
        session_id="session-iso",  # 会话 ID
        parent_run_id="master-run-a",  # 父 run ID
        tool_call_id="call-a",  # 工具调用 ID
        subagent_type="Plan",  # 子代理类型
        child_id="plan-child-a",  # child 稳定标识（手动指定，便于验证）
        prompt="分析需求 A",  # 任务 prompt
        description="分析需求 A",  # 任务描述
        metadata=None,  # 无请求元数据
        cancel_event=None,  # 无外部取消事件
    )

    # 第二次执行：派发 Plan 处理"需求 B"，使用不同的运行时输出
    runtime_b = FakeAgentRuntime(  # 子代理 B 的假运行时
        turn_results=[
            ["针对需求 B 的计划。", TurnComplete()],  # 返回针对需求 B 的计划文本
        ]
    )
    profile_b = AgentExecutionProfile(  # 子代理 B 的执行配置
        agent_id="Plan",  # profile 关联的 agent_id
        agent=child_agent,  # 子代理静态配置
        prompt_source=AgentPromptSource(kind="file", path="plan.md"),  # prompt 来源
        runtime=runtime_b,  # 假运行时
        tool_registry=ToolRegistry(),  # 空工具注册表
        tool_hook_pipeline=ToolHookPipeline(),  # 空 Hook 管线
        max_turns=3,  # 最大轮数
    )
    runner_b = ChildAgentRunner(  # 子代理 B 的执行服务
        session_store=session_store,  # 会话存储
        run_store=run_store,  # 运行存储
        redis=fake_redis,  # Redis 客户端
        agent_loop=agent_loop,  # AgentLoop 编排器
        child_profiles={"Plan": profile_b},  # 注册子代理 profile
        settings=child_settings,  # 应用配置
    )
    # 直接调用 run_child 执行子代理 B
    result_b = await runner_b.run_child(
        session_id="session-iso",  # 会话 ID
        parent_run_id="master-run-b",  # 父 run ID
        tool_call_id="call-b",  # 工具调用 ID
        subagent_type="Plan",  # 子代理类型
        child_id="plan-child-b",  # child 稳定标识（手动指定，与 A 不同）
        prompt="分析需求 B",  # 任务 prompt
        description="分析需求 B",  # 任务描述
        metadata=None,  # 无请求元数据
        cancel_event=None,  # 无外部取消事件
    )

    # ========== 验证两个 child 的输出互不相同 ==========
    assert "需求 A" in result_a.output, f"child A 输出应包含'需求 A'，实际: {result_a.output}"
    assert "需求 B" in result_b.output, f"child B 输出应包含'需求 B'，实际: {result_b.output}"
    assert result_a.child_id != result_b.child_id, "两个 child 应有不同的 child_id"

    # ========== 验证 session_children 包含两个 child_id ==========
    child_ids = await session_store.list_session_children("session-iso")  # 读取所有 child_id
    assert "plan-child-a" in child_ids, "session_children 应包含 plan-child-a"
    assert "plan-child-b" in child_ids, "session_children 应包含 plan-child-b"

    # ========== 验证 child A 的上下文不包含 child B 的内容 ==========
    messages_a = await session_store.list_child_messages("session-iso", "plan-child-a")  # 读取 child A 上下文
    messages_b = await session_store.list_child_messages("session-iso", "plan-child-b")  # 读取 child B 上下文

    # child A 消息全量
    content_a_full = " ".join(m.content or "" for m in messages_a)  # 拼接 child A 所有消息内容
    # child B 消息全量
    content_b_full = " ".join(m.content or "" for m in messages_b)  # 拼接 child B 所有消息内容

    # child A 不应包含 child B 特有的内容
    assert "需求 B" not in content_a_full, "child A 的上下文不应包含需求 B 的内容"
    # child B 不应包含 child A 特有的内容
    assert "需求 A" not in content_b_full, "child B 的上下文不应包含需求 A 的内容"


@pytest.mark.asyncio
async def test_resumable_subagent_query_returns_latest_description(fake_redis):
    """测试首次派发与 resume 后，查询工具返回同一 child 的最新 description。"""
    session_store = RedisSessionStore(fake_redis, key_prefix="test")
    run_store = RedisRunStore(fake_redis, key_prefix="test")
    await session_store.create_session(
        Session(
            session_id="session-query",
            agent_id="master-agent",
            created_at=datetime.now(timezone.utc),
        )
    )

    runtime = FakeAgentRuntime(turn_results=[["第一次输出", TurnComplete()], ["第二次输出", TurnComplete()]])
    child_agent = Agent(
        agent_id="Plan",
        name="Plan",
        model="gpt-4.1-mini",
        system_prompt="你是计划代理。",
        temperature=0.2,
    )
    child_profile = AgentExecutionProfile(
        agent_id="Plan",
        agent=child_agent,
        prompt_source=AgentPromptSource(kind="file", path="plan.md"),
        runtime=runtime,
        tool_registry=ToolRegistry(),
        tool_hook_pipeline=ToolHookPipeline(),
        max_turns=3,
    )
    runner = ChildAgentRunner(
        session_store=session_store,
        run_store=run_store,
        redis=fake_redis,
        agent_loop=AgentLoop(),
        child_profiles={"Plan": child_profile},
        settings=Settings(redis_url="redis://localhost:6379/0"),
    )

    await runner.run_child(
        session_id="session-query",
        parent_run_id="master-run-1",
        tool_call_id="call-1",
        subagent_type="Plan",
        child_id="plan-query",
        prompt="第一次任务",
        description="第一次描述",
        metadata=None,
        cancel_event=None,
    )
    await runner.run_child(
        session_id="session-query",
        parent_run_id="master-run-2",
        tool_call_id="call-2",
        subagent_type="Plan",
        child_id="plan-query",
        prompt="第二次任务",
        description="第二次描述",
        metadata=None,
        cancel_event=None,
        is_resume=True,
    )

    summaries = await session_store.list_session_child_summaries("session-query")

    assert summaries == [
        SessionChildSummary(
            resume_id="plan-query",
            subagent_type="Plan",
            description="第二次描述",
        )
    ]

    # 通过 ListResumableSubagentsTool 验证完整工具链路输出
    list_tool = ListResumableSubagentsTool(session_store)
    tool_context = ExecutionContext(
        run_id="verify-run",
        session_id="session-query",
        metadata=None,
        agent=child_agent,
        run_type="master",
    )
    tool_result = await list_tool.call({}, tool_context)
    items = json.loads(tool_result.content)["items"]
    assert items == [
        {"resume_id": "plan-query", "subagent_type": "Plan", "description": "第二次描述"},
    ]
