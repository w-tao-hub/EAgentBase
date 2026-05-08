"""ContextBuilder 的单元测试。"""

from __future__ import annotations  # 启用未来注解，避免前向引用问题

from datetime import datetime, timezone  # 导入日期时间类和 UTC 时区

import pytest  # 导入 pytest 测试框架

from app.core.models.agent import Agent  # 导入 Agent 模型
from app.core.models.stored_message import StoredMessage  # 导入会话消息模型
from app.core.runtime.context_builder import ContextBuilder, SummaryPersistenceTarget, TokenBudgetCompressionPolicy  # 导入上下文构建器与压缩策略
from app.infra.store.redis_session_store import RedisSessionStore  # 导入 Redis 会话存储
from tests.fakes import FakeLLMAdapter  # 导入测试用假的 LiteLLM 适配器


@pytest.mark.asyncio
async def test_context_builder_includes_system_history_and_current_user_message():
    """测试 ContextBuilder 能正确组装 system + 历史消息 + 当前 user message。"""
    # 创建一个测试用的 Agent 实例
    agent = Agent(
        agent_id="test-agent",  # Agent 唯一标识
        name="Test Agent",  # Agent 显示名称
        model="gpt-4.1-mini",  # 使用的模型
        system_prompt="你是一个乐于助人的助手。",  # 系统提示词
        temperature=0.2,  # 采样温度
    )

    # 创建历史消息：用户之前的提问
    old_user = StoredMessage.create(
        role="user",  # 消息角色为用户
        content="你好",  # 消息内容
        timestamp=datetime(2026, 4, 3, 10, 0, 0, tzinfo=timezone.utc),  # 消息时间戳
    )

    # 创建历史消息：助手之前的回复
    old_assistant = StoredMessage.create(
        role="assistant",  # 消息角色为助手
        content="你好！有什么可以帮助你的吗？",  # 消息内容
        timestamp=datetime(2026, 4, 3, 10, 0, 1, tzinfo=timezone.utc),  # 消息时间戳
    )

    # 创建当前用户消息
    current_user = StoredMessage.create(
        role="user",  # 消息角色为用户
        content="今天天气怎么样？",  # 消息内容
        timestamp=datetime(2026, 4, 3, 10, 1, 0, tzinfo=timezone.utc),  # 消息时间戳
    )

    # 创建 ContextBuilder 实例
    builder = ContextBuilder()

    # 调用 build 方法构建消息列表
    messages = await builder.build(
        agent=agent,  # Agent 配置
        history=[old_user, old_assistant],  # 历史消息列表
        current_user_message=current_user,  # 当前用户消息
    )

    # 断言消息列表的角色顺序正确：system -> user -> assistant -> user
    assert [message.role for message in messages] == ["system", "user", "assistant", "user"]

    # 断言第一条消息是 system 提示词
    assert messages[0].role == "system"  # 验证角色为 system
    assert messages[0].content == "你是一个乐于助人的助手。"  # 验证内容来自 Agent 的 system_prompt

    # 断言第二条消息是历史用户消息
    assert messages[1].role == "user"  # 验证角色为 user
    assert messages[1].content == "你好"  # 验证内容正确

    # 断言第三条消息是历史助手消息
    assert messages[2].role == "assistant"  # 验证角色为 assistant
    assert messages[2].content == "你好！有什么可以帮助你的吗？"  # 验证内容正确

    # 断言第四条消息是当前用户消息
    assert messages[3].role == "user"  # 验证角色为 user
    assert messages[3].content == "今天天气怎么样？"  # 验证内容正确


@pytest.mark.asyncio
async def test_context_builder_with_empty_history():
    """测试 ContextBuilder 在空历史消息时仍能正确组装。"""
    # 创建一个测试用的 Agent 实例
    agent = Agent(
        agent_id="test-agent",  # Agent 唯一标识
        name="Test Agent",  # Agent 显示名称
        model="gpt-4.1-mini",  # 使用的模型
        system_prompt="你是一个乐于助人的助手。",  # 系统提示词
        temperature=0.2,  # 采样温度
    )

    # 创建当前用户消息
    current_user = StoredMessage.create(
        role="user",  # 消息角色为用户
        content="你好",  # 消息内容
        timestamp=datetime(2026, 4, 3, 10, 0, 0, tzinfo=timezone.utc),  # 消息时间戳
    )

    # 创建 ContextBuilder 实例
    builder = ContextBuilder()

    # 调用 build 方法构建消息列表，历史消息为空列表
    messages = await builder.build(
        agent=agent,  # Agent 配置
        history=[],  # 空历史消息列表
        current_user_message=current_user,  # 当前用户消息
    )

    # 断言消息列表只有 system 和当前 user 消息
    assert [message.role for message in messages] == ["system", "user"]

    # 断言第一条消息是 system 提示词
    assert messages[0].role == "system"  # 验证角色为 system
    assert messages[0].content == "你是一个乐于助人的助手。"  # 验证内容正确

    # 断言第二条消息是当前用户消息
    assert messages[1].role == "user"  # 验证角色为 user
    assert messages[1].content == "你好"  # 验证内容正确


@pytest.mark.asyncio
async def test_context_builder_build_llm_messages_returns_provider_ready_messages():
    """测试 ContextBuilder 能直接生成给 LLM 使用的 dict 消息列表。"""
    # 创建一个测试用的 Agent 实例。
    agent = Agent(
        agent_id="test-agent",  # Agent 唯一标识
        name="Test Agent",  # Agent 显示名称
        model="gpt-4.1-mini",  # 模型名称
        system_prompt="你是一个乐于助人的助手。",  # 系统提示词
        temperature=0.2,  # 温度参数
    )

    # 构造一条历史用户消息。
    old_user = StoredMessage.create(
        role="user",  # 历史角色为用户
        content="你好",  # 历史消息内容
        timestamp=datetime(2026, 4, 3, 10, 0, 0, tzinfo=timezone.utc),  # 历史时间戳
    )

    # 构造一条当前用户消息。
    current_user = StoredMessage.create(
        role="user",  # 当前角色为用户
        content="今天天气怎么样？",  # 当前消息内容
        timestamp=datetime(2026, 4, 3, 10, 1, 0, tzinfo=timezone.utc),  # 当前时间戳
    )

    # 调用新的上下文准备入口，直接构建可给 LLM 使用的消息结构。
    result = await ContextBuilder.build_llm_messages_with_repair_meta(
        agent=agent,  # 传入 Agent 配置
        history=[old_user],  # 传入历史消息
        current_user_message=current_user,  # 传入当前用户消息
    )
    messages = result.llm_messages  # 读取最终可直接发给模型的消息列表。

    # 验证顺序仍为 system -> history -> current user。
    assert messages == [
        {"role": "system", "content": "你是一个乐于助人的助手。"},
        {"role": "user", "content": "你好"},
        {"role": "user", "content": "今天天气怎么样？"},
    ]


@pytest.mark.asyncio
async def test_context_builder_supports_injected_trim_policy():
    """测试 ContextBuilder 能接入自定义上下文策略。"""
    # 创建一个测试用的 Agent 实例。
    agent = Agent(
        agent_id="test-agent",  # Agent 唯一标识
        name="Test Agent",  # Agent 显示名称
        model="gpt-4.1-mini",  # 模型名称
        system_prompt="你是一个乐于助人的助手。",  # 系统提示词
        temperature=0.2,  # 温度参数
    )

    # 构造历史消息，后续用于验证策略是否拿到了完整输入。
    old_user = StoredMessage.create(
        role="user",  # 历史角色为用户
        content="你好",  # 历史消息内容
        timestamp=datetime(2026, 4, 3, 10, 0, 0, tzinfo=timezone.utc),  # 历史时间戳
    )
    old_assistant = StoredMessage.create(
        role="assistant",  # 历史角色为助手
        content="你好！",  # 历史助手内容
        timestamp=datetime(2026, 4, 3, 10, 0, 1, tzinfo=timezone.utc),  # 历史时间戳
    )

    # 构造当前用户消息。
    current_user = StoredMessage.create(
        role="user",  # 当前角色为用户
        content="继续说",  # 当前消息内容
        timestamp=datetime(2026, 4, 3, 10, 1, 0, tzinfo=timezone.utc),  # 当前时间戳
    )

    class OnlyLatestUserPolicy:
        """仅保留 system 和当前用户消息的测试策略。"""

        def __init__(self) -> None:
            """初始化测试策略。"""
            self.called = False  # 记录是否被调用
            self.last_history: list[StoredMessage] | None = None  # 记录传入历史
            self.last_current_user: StoredMessage | None = None  # 记录传入当前用户消息

        async def build_messages(
            self,
            *,
            agent: Agent,
            system_message: StoredMessage,
            history: list[StoredMessage],
            history_indices: list[int] | None = None,
            current_user_message: StoredMessage | None,
            session_id: str | None = None,
            extra_system_messages: list[str] | None = None,
        ) -> list[StoredMessage]:
            """返回策略处理后的消息列表。"""
            del agent, history_indices, session_id, extra_system_messages
            self.called = True  # 标记策略已被调用
            self.last_history = history  # 记录原始历史
            self.last_current_user = current_user_message  # 记录当前用户消息
            return [system_message, current_user_message]  # 仅保留 system 和当前用户消息

    policy = OnlyLatestUserPolicy()  # 创建策略实例

    # 使用自定义策略构建给 LLM 的消息列表。
    result = await ContextBuilder.build_llm_messages_with_repair_meta(
        agent=agent,  # 传入 Agent 配置
        history=[old_user, old_assistant],  # 传入完整历史
        current_user_message=current_user,  # 传入当前用户消息
        trim_policy=policy,  # 传入自定义策略
    )
    messages = result.llm_messages  # 读取最终可直接发给模型的消息列表。

    # 验证策略被真正调用，且能拿到完整输入上下文。
    assert policy.called is True  # 验证策略被调用
    assert policy.last_history == [old_user, old_assistant]  # 验证完整历史被传入策略
    assert policy.last_current_user == current_user  # 验证当前用户消息被传入策略

    # 验证 ContextBuilder 使用了策略返回的结果继续输出给 LLM。
    assert messages == [
        {"role": "system", "content": "你是一个乐于助人的助手。"},
        {"role": "user", "content": "继续说"},
    ]


@pytest.mark.asyncio
async def test_context_builder_build_llm_messages_inserts_extra_system_messages_after_main_system_prompt():
    """测试额外 system 提醒会插入主 system prompt 之后。"""
    agent = Agent(
        agent_id="test-agent",
        name="Test Agent",
        model="gpt-4.1-mini",
        system_prompt="主系统提示词",
        temperature=0.2,
    )

    history = [
        StoredMessage.create(
            role="user",
            content="你好",
            timestamp=datetime(2026, 4, 3, 10, 0, 0, tzinfo=timezone.utc),
        )
    ]

    result = await ContextBuilder.build_llm_messages_with_repair_meta(
        agent=agent,
        history=history,
        extra_system_messages=["可用技能：demo"],
    )
    messages = result.llm_messages  # 读取最终可直接发给模型的消息列表。

    assert messages == [
        {"role": "system", "content": "主系统提示词"},
        {"role": "system", "content": "可用技能：demo"},
        {"role": "user", "content": "你好"},
    ]


@pytest.mark.asyncio
async def test_context_builder_drops_assistant_tool_calls_without_results_and_marks_history_dirty():
    """测试缺失 tool 结果时，会删除未配对的 assistant tool_calls 并标记脏历史。"""
    agent = Agent(
        agent_id="test-agent",
        name="Test Agent",
        model="gpt-4.1-mini",
        system_prompt="主系统提示词",
        temperature=0.2,
    )

    history = [
        StoredMessage.create(
            role="assistant",
            content=None,
            timestamp=datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc),
            tool_calls=[
                {
                    "id": "call-missing",
                    "type": "function",
                    "function": {"name": "search", "arguments": "{}"},
                }
            ],
        ),
        StoredMessage.create(
            role="user",
            content="继续",
            timestamp=datetime(2026, 4, 10, 10, 0, 1, tzinfo=timezone.utc),
        ),
    ]

    result = await ContextBuilder.build_llm_messages_with_repair_meta(
        agent=agent,
        history=history,
    )

    assert result.history_dirty is True
    assert result.llm_messages == [
        {"role": "system", "content": "主系统提示词"},
        {"role": "user", "content": "继续"},
    ]


@pytest.mark.asyncio
async def test_context_builder_downgrades_assistant_to_plain_text_when_all_tool_calls_are_dropped():
    """测试 assistant 既有文本又有丢失工具结果时，会降级为普通 assistant。"""
    agent = Agent(
        agent_id="test-agent",
        name="Test Agent",
        model="gpt-4.1-mini",
        system_prompt="主系统提示词",
        temperature=0.2,
    )

    history = [
        StoredMessage.create(
            role="assistant",
            content="我先解释一下",
            timestamp=datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc),
            tool_calls=[
                {
                    "id": "call-missing",
                    "type": "function",
                    "function": {"name": "search", "arguments": "{}"},
                }
            ],
        )
    ]

    result = await ContextBuilder.build_llm_messages_with_repair_meta(
        agent=agent,
        history=history,
    )

    assert result.history_dirty is True
    assert result.llm_messages == [
        {"role": "system", "content": "主系统提示词"},
        {"role": "assistant", "content": "我先解释一下"},
    ]


@pytest.mark.asyncio
async def test_context_builder_keeps_reasoning_content_on_assistant_history():
    """测试带 reasoning_content 的 assistant 历史会被原样拼回模型上下文。"""
    agent = Agent(
        agent_id="test-agent",
        name="Test Agent",
        model="deepseek/deepseek-v4-flash",
        system_prompt="主系统提示词",
        temperature=0.2,
    )

    history = [
        StoredMessage.create(
            role="assistant",
            content="结论文本",
            reasoning_content="这是上一轮思考内容",
            timestamp=datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc),
        ),
    ]

    result = await ContextBuilder.build_llm_messages_with_repair_meta(
        agent=agent,
        history=history,
    )

    assert result.llm_messages == [
        {"role": "system", "content": "主系统提示词"},
        {
            "role": "assistant",
            "content": "结论文本",
            "reasoning_content": "这是上一轮思考内容",
        },
    ]


@pytest.mark.asyncio
async def test_context_builder_injects_synthetic_assistant_for_orphan_tool_result():
    """测试孤儿 tool 结果会在前面补一条合成 assistant(tool_calls)。"""
    agent = Agent(
        agent_id="test-agent",
        name="Test Agent",
        model="gpt-4.1-mini",
        system_prompt="主系统提示词",
        temperature=0.2,
    )

    history = [
        StoredMessage.create(
            role="tool",
            content="工具结果",
            timestamp=datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc),
            tool_call_id="call-orphan",
            name="search",
        )
    ]

    result = await ContextBuilder.build_llm_messages_with_repair_meta(
        agent=agent,
        history=history,
    )

    assert result.history_dirty is True
    assert result.llm_messages == [
        {"role": "system", "content": "主系统提示词"},
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
            "content": "工具结果",
            "tool_call_id": "call-orphan",
            "name": "search",
        },
    ]


@pytest.mark.asyncio
async def test_context_builder_marks_history_dirty_when_tool_message_has_no_tool_call_id():
    """测试缺少 tool_call_id 的 tool 消息会被丢弃并标记脏历史。"""
    agent = Agent(
        agent_id="test-agent",
        name="Test Agent",
        model="gpt-4.1-mini",
        system_prompt="主系统提示词",
        temperature=0.2,
    )

    history = [
        StoredMessage.create(
            role="tool",
            content="无效工具结果",
            timestamp=datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc),
            name="search",
        )
    ]

    result = await ContextBuilder.build_llm_messages_with_repair_meta(
        agent=agent,
        history=history,
    )

    assert result.history_dirty is True
    assert result.llm_messages == [
        {"role": "system", "content": "主系统提示词"},
    ]


@pytest.mark.asyncio
async def test_context_builder_keeps_assistant_tool_call_before_matched_tool_result():
    """测试合法配对时 assistant(tool_calls) 必须出现在对应 tool 结果之前。"""
    agent = Agent(
        agent_id="test-agent",
        name="Test Agent",
        model="gpt-4.1-mini",
        system_prompt="主系统提示词",
        temperature=0.2,
    )

    history = [
        StoredMessage.create(
            role="assistant",
            content="我先调用工具",
            timestamp=datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc),
            tool_calls=[
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "task_get", "arguments": "{}"},
                }
            ],
        ),
        StoredMessage.create(
            role="tool",
            content='{"id":"1"}',
            timestamp=datetime(2026, 4, 10, 10, 0, 1, tzinfo=timezone.utc),
            tool_call_id="call-1",
            name="task_get",
        ),
    ]

    result = await ContextBuilder.build_llm_messages_with_repair_meta(
        agent=agent,
        history=history,
    )

    assert result.history_dirty is False
    assert result.llm_messages == [
        {"role": "system", "content": "主系统提示词"},
        {
            "role": "assistant",
            "content": "我先调用工具",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "task_get", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "content": '{"id":"1"}',
            "tool_call_id": "call-1",
            "name": "task_get",
        },
    ]


@pytest.mark.asyncio
async def test_token_budget_compression_policy_removes_old_query_tool_result_and_skill_messages(fake_redis):
    """测试压缩策略会删除最近两轮之前的 query_tool_result 与 skill 注入痕迹。"""
    session_store = RedisSessionStore(fake_redis, key_prefix="test")
    llm_adapter = FakeLLMAdapter(prompt_token_counts=[80, 30])
    policy = TokenBudgetCompressionPolicy(
        session_store=session_store,
        llm_adapter=llm_adapter,
        token_threshold=60,
    )
    agent = Agent(
        agent_id="test-agent",
        name="Test Agent",
        model="gpt-4.1-mini",
        system_prompt="主系统提示词",
        temperature=0.2,
    )
    history = [
        StoredMessage.create(
            role="user",
            content="第一轮问题",
            timestamp=datetime(2026, 4, 11, 9, 59, 0, tzinfo=timezone.utc),
        ),
        StoredMessage.create(
            role="assistant",
            content="先查完整结果",
            timestamp=datetime(2026, 4, 11, 10, 0, 0, tzinfo=timezone.utc),
            tool_calls=[
                {
                    "id": "call-query",
                    "type": "function",
                    "function": {"name": "query_tool_result", "arguments": '{"key":"1"}'},
                }
            ],
        ),
        StoredMessage.create(
            role="tool",
            content="完整工具结果",
            timestamp=datetime(2026, 4, 11, 10, 0, 1, tzinfo=timezone.utc),
            tool_call_id="call-query",
            name="query_tool_result",
        ),
        StoredMessage.create(
            role="user",
            content="<skill_name>demo</skill_name><skill_message>full</skill_message>",
            timestamp=datetime(2026, 4, 11, 10, 0, 2, tzinfo=timezone.utc),
            is_meta=True,
        ),
        StoredMessage.create(
            role="user",
            content="第二轮问题",
            timestamp=datetime(2026, 4, 11, 10, 1, 0, tzinfo=timezone.utc),
        ),
        StoredMessage.create(
            role="assistant",
            content="第二轮回答",
            timestamp=datetime(2026, 4, 11, 10, 1, 1, tzinfo=timezone.utc),
        ),
        StoredMessage.create(
            role="user",
            content="第三轮问题",
            timestamp=datetime(2026, 4, 11, 10, 2, 0, tzinfo=timezone.utc),
        ),
    ]

    result = await ContextBuilder.build_llm_messages_with_repair_meta(
        agent=agent,
        history=history,
        trim_policy=policy,
    )
    messages = result.llm_messages  # 读取最终可直接发给模型的消息列表。

    assert messages == [
        {"role": "system", "content": "主系统提示词"},
        {"role": "user", "content": "第一轮问题"},
        {"role": "assistant", "content": "先查完整结果"},
        {"role": "user", "content": "第二轮问题"},
        {"role": "assistant", "content": "第二轮回答"},
        {"role": "user", "content": "第三轮问题"},
    ]


@pytest.mark.asyncio
async def test_token_budget_compression_policy_persists_summary_and_keeps_recent_two_rounds(fake_redis):
    """测试压缩策略会落库存摘要，并在当前轮返回摘要加最近两轮完整会话。"""
    session_store = RedisSessionStore(fake_redis, key_prefix="test")
    llm_adapter = FakeLLMAdapter(
        prompt_token_counts=[90, 90, 100],
        completion_text="这是压缩后的历史摘要",
    )
    policy = TokenBudgetCompressionPolicy(
        session_store=session_store,
        llm_adapter=llm_adapter,
        token_threshold=60,
    )
    agent = Agent(
        agent_id="test-agent",
        name="Test Agent",
        model="gpt-4.1-mini",
        system_prompt="主系统提示词",
        temperature=0.2,
    )
    history = [
        StoredMessage.create(
            role="user",
            content="第一轮问题",
            timestamp=datetime(2026, 4, 11, 10, 0, 0, tzinfo=timezone.utc),
        ),
        StoredMessage.create(
            role="assistant",
            content="第一轮回答",
            timestamp=datetime(2026, 4, 11, 10, 0, 1, tzinfo=timezone.utc),
        ),
        StoredMessage.create(
            role="user",
            content="第二轮问题",
            timestamp=datetime(2026, 4, 11, 10, 1, 0, tzinfo=timezone.utc),
        ),
        StoredMessage.create(
            role="assistant",
            content="第二轮回答",
            timestamp=datetime(2026, 4, 11, 10, 1, 1, tzinfo=timezone.utc),
        ),
        StoredMessage.create(
            role="user",
            content="第三轮问题",
            timestamp=datetime(2026, 4, 11, 10, 2, 0, tzinfo=timezone.utc),
        ),
        StoredMessage.create(
            role="assistant",
            content="第三轮回答",
            timestamp=datetime(2026, 4, 11, 10, 2, 1, tzinfo=timezone.utc),
        ),
    ]
    for message in history:
        await session_store.append_message("session-1", message)

    result = await ContextBuilder.build_llm_messages_with_repair_meta(
        agent=agent,
        history=history,
        trim_policy=policy,
        session_id="session-1",
        current_user_message=StoredMessage.create(
            role="user",
            content="第四轮问题",
            timestamp=datetime(2026, 4, 11, 10, 3, 0, tzinfo=timezone.utc),
        ),
    )
    messages = result.llm_messages  # 读取最终可直接发给模型的消息列表。

    assert messages == [
        {"role": "system", "content": "主系统提示词"},
        {"role": "user", "content": "<context_summary>这是压缩后的历史摘要</context_summary>"},
        {"role": "user", "content": "第二轮问题"},
        {"role": "assistant", "content": "第二轮回答"},
        {"role": "user", "content": "第三轮问题"},
        {"role": "assistant", "content": "第三轮回答"},
        {"role": "user", "content": "第四轮问题"},
    ]

    active_messages = await session_store.list_active_messages("session-1")

    assert [message.content for message in active_messages] == [
        "<context_summary>这是压缩后的历史摘要</context_summary>",
        "第二轮问题",
        "第二轮回答",
        "第三轮问题",
        "第三轮回答",
    ]
    assert llm_adapter.last_completion_call is not None
    assert llm_adapter.last_completion_call["enable_thinking"] is False


@pytest.mark.asyncio
async def test_token_budget_compression_policy_persists_child_summary_to_child_context(fake_redis):
    """测试 child 压缩只会写 child 摘要路径，不会污染主会话摘要路径。"""
    session_store = RedisSessionStore(fake_redis, key_prefix="test")
    llm_adapter = FakeLLMAdapter(
        prompt_token_counts=[90, 90, 100],
        completion_text="child 摘要",
    )
    policy = TokenBudgetCompressionPolicy(
        session_store=session_store,
        llm_adapter=llm_adapter,
        token_threshold=60,
    )
    agent = Agent(
        agent_id="test-agent",
        name="Test Agent",
        model="gpt-4.1-mini",
        system_prompt="主系统提示词",
        temperature=0.2,
    )
    history = [
        StoredMessage.create(role="user", content="第一轮问题", timestamp=datetime(2026, 4, 11, 10, 0, 0, tzinfo=timezone.utc)),
        StoredMessage.create(role="assistant", content="第一轮回答", timestamp=datetime(2026, 4, 11, 10, 0, 1, tzinfo=timezone.utc)),
        StoredMessage.create(role="user", content="第二轮问题", timestamp=datetime(2026, 4, 11, 10, 1, 0, tzinfo=timezone.utc)),
        StoredMessage.create(role="assistant", content="第二轮回答", timestamp=datetime(2026, 4, 11, 10, 1, 1, tzinfo=timezone.utc)),
        StoredMessage.create(role="user", content="第三轮问题", timestamp=datetime(2026, 4, 11, 10, 2, 0, tzinfo=timezone.utc)),
        StoredMessage.create(role="assistant", content="第三轮回答", timestamp=datetime(2026, 4, 11, 10, 2, 1, tzinfo=timezone.utc)),
    ]
    for message in history:
        await session_store.append_child_message("session-1", "writer-1", message, source_run_id="run-child-1", subagent_type="Plan")

    result = await ContextBuilder.build_llm_messages_with_repair_meta(
        agent=agent,
        history=history,
        trim_policy=policy,
        session_id="session-1",
        summary_target=SummaryPersistenceTarget.for_child("session-1", "writer-1"),
        current_user_message=StoredMessage.create(
            role="user",
            content="第四轮问题",
            timestamp=datetime(2026, 4, 11, 10, 3, 0, tzinfo=timezone.utc),
            child_id="writer-1",
            subagent_type="Plan",
        ),
    )

    assert result.llm_messages[1]["content"] == "<context_summary>child 摘要</context_summary>"
    assert await session_store.get_main_context_summary_state("session-1") is None
    child_state = await session_store.get_child_context_summary_state("session-1", "writer-1")
    assert child_state is not None
    child_active_messages = await session_store.list_child_active_messages("session-1", "writer-1")
    assert child_active_messages[0].content == "<context_summary>child 摘要</context_summary>"


@pytest.mark.asyncio
async def test_token_budget_compression_policy_skips_compression_when_reasoning_content_exists(fake_redis):
    """测试存在 reasoning_content 历史时，压缩策略直接跳过摘要流程。"""
    session_store = RedisSessionStore(fake_redis, key_prefix="test")
    llm_adapter = FakeLLMAdapter(
        prompt_token_counts=[90],
        completion_text="不应被使用",
    )
    policy = TokenBudgetCompressionPolicy(
        session_store=session_store,
        llm_adapter=llm_adapter,
        token_threshold=60,
    )
    agent = Agent(
        agent_id="test-agent",
        name="Test Agent",
        model="deepseek/deepseek-v4-flash",
        system_prompt="主系统提示词",
        temperature=0.2,
    )
    history = [
        StoredMessage.create(
            role="user",
            content="第一轮问题",
            timestamp=datetime(2026, 4, 11, 10, 0, 0, tzinfo=timezone.utc),
        ),
        StoredMessage.create(
            role="assistant",
            content="第一轮回答",
            reasoning_content="必须原样回放的思考内容",
            timestamp=datetime(2026, 4, 11, 10, 0, 1, tzinfo=timezone.utc),
        ),
    ]

    result = await ContextBuilder.build_llm_messages_with_repair_meta(
        agent=agent,
        history=history,
        trim_policy=policy,
    )

    assert result.llm_messages == [
        {"role": "system", "content": "主系统提示词"},
        {"role": "user", "content": "第一轮问题"},
        {
            "role": "assistant",
            "content": "第一轮回答",
            "reasoning_content": "必须原样回放的思考内容",
        },
    ]
    assert llm_adapter.last_completion_call is None


@pytest.mark.asyncio
async def test_token_budget_compression_policy_retries_summary_once_then_raises(fake_redis):
    """测试摘要调用第一次失败会重试一次，第二次仍失败则直接抛错。"""
    session_store = RedisSessionStore(fake_redis, key_prefix="test")
    llm_adapter = FakeLLMAdapter(
        prompt_token_counts=[90, 90],
        completion_errors=[RuntimeError("boom-1"), RuntimeError("boom-2")],
    )
    policy = TokenBudgetCompressionPolicy(
        session_store=session_store,
        llm_adapter=llm_adapter,
        token_threshold=60,
    )
    agent = Agent(
        agent_id="test-agent",
        name="Test Agent",
        model="gpt-4.1-mini",
        system_prompt="主系统提示词",
        temperature=0.2,
    )
    history = [
        StoredMessage.create(
            role="user",
            content="第一轮问题",
            timestamp=datetime(2026, 4, 11, 10, 0, 0, tzinfo=timezone.utc),
        ),
        StoredMessage.create(
            role="assistant",
            content="第一轮回答",
            timestamp=datetime(2026, 4, 11, 10, 0, 1, tzinfo=timezone.utc),
        ),
        StoredMessage.create(
            role="user",
            content="第二轮问题",
            timestamp=datetime(2026, 4, 11, 10, 1, 0, tzinfo=timezone.utc),
        ),
        StoredMessage.create(
            role="assistant",
            content="第二轮回答",
            timestamp=datetime(2026, 4, 11, 10, 1, 1, tzinfo=timezone.utc),
        ),
        StoredMessage.create(
            role="user",
            content="第三轮问题",
            timestamp=datetime(2026, 4, 11, 10, 2, 0, tzinfo=timezone.utc),
        ),
    ]

    with pytest.raises(Exception, match="上下文摘要生成失败"):
        await ContextBuilder.build_llm_messages_with_repair_meta(
            agent=agent,
            history=history,
            trim_policy=policy,
        )


@pytest.mark.asyncio
async def test_token_budget_compression_policy_keeps_stable_offsets_after_repeated_compression(fake_redis):
    """连续两次压缩后，摘要边界中的活动起点偏移仍应保持完整历史绝对索引。"""
    session_store = RedisSessionStore(fake_redis, key_prefix="test")
    llm_adapter = FakeLLMAdapter(
        prompt_token_counts=[120, 120, 40, 130, 130, 40],
        completion_text="压缩摘要",
    )
    policy = TokenBudgetCompressionPolicy(
        session_store=session_store,
        llm_adapter=llm_adapter,
        token_threshold=60,
    )
    agent = Agent(
        agent_id="agent-1",
        name="Agent",
        model="gpt-4.1-mini",
        system_prompt="主系统提示词",
        temperature=0.2,
    )

    def _round(q: str, a: str, base_hour: int = 10, base_minute: int = 0) -> list[StoredMessage]:
        """构造一轮测试消息（一问一答）。"""
        return [
            StoredMessage.create(role="user", content=q, timestamp=datetime(2026, 5, 6, base_hour, base_minute, 0, tzinfo=timezone.utc)),
            StoredMessage.create(role="assistant", content=a, timestamp=datetime(2026, 5, 6, base_hour, base_minute, 1, tzinfo=timezone.utc)),
        ]

    # 先追加 4 轮（R1-R4），触发第一次压缩。
    initial_rounds = _round("第一轮问题", "第一轮回答", 10, 0) + _round("第二轮问题", "第二轮回答", 10, 1) + _round("第三轮问题", "第三轮回答", 10, 2) + _round("第四轮问题", "第四轮回答", 10, 3)
    for message in initial_rounds:
        await session_store.append_main_message("session-repeat", message)

    # 第一次压缩。
    await ContextBuilder.build_llm_messages_with_repair_meta(
        agent=agent,
        history=initial_rounds,
        trim_policy=policy,
        session_id="session-repeat",
        summary_target=SummaryPersistenceTarget.for_main("session-repeat"),
    )

    # 追加更多轮次（R5-R6）后，使第二次压缩时有足够可压缩历史。
    r5q = StoredMessage.create(role="user", content="第五轮问题", timestamp=datetime(2026, 5, 6, 10, 4, 0, tzinfo=timezone.utc))
    r5a = StoredMessage.create(role="assistant", content="第五轮回答", timestamp=datetime(2026, 5, 6, 10, 4, 1, tzinfo=timezone.utc))
    r6q = StoredMessage.create(role="user", content="第六轮问题", timestamp=datetime(2026, 5, 6, 10, 5, 0, tzinfo=timezone.utc))
    r6a = StoredMessage.create(role="assistant", content="第六轮回答", timestamp=datetime(2026, 5, 6, 10, 5, 1, tzinfo=timezone.utc))
    extra_rounds = [r5q, r5a, r6q, r6a]
    for message in extra_rounds:
        await session_store.append_main_message("session-repeat", message)

    # 读取压缩后的活动窗口（含新追加的轮次），用于第二次压缩。
    updated_history, updated_indices = await session_store.list_active_main_messages_with_indices("session-repeat")
    r5q_message_id = r5q.message_id  # 记录 R5Q 的 message_id，后续精确断言。

    # 第二次压缩。
    await ContextBuilder.build_llm_messages_with_repair_meta(
        agent=agent,
        history=updated_history,
        history_indices=updated_indices,
        trim_policy=policy,
        session_id="session-repeat",
        summary_target=SummaryPersistenceTarget.for_main("session-repeat"),
    )

    # 验证两次压缩后，摘要边界状态仍然精确稳定。
    state = await session_store.get_main_context_summary_state("session-repeat")

    assert state is not None
    # 1) active_start_offset 精确等于 R5Q 在 Redis List 中的全局绝对索引（9）。
    assert state.active_start_offset == 9
    # 2) active_start_message_id 精确等于 R5Q 的 message_id。
    assert state.active_start_message_id == r5q_message_id
    # summary_offset 在第二次压缩后应更新为 13（8 条初始消息 + 1 条第一次摘要 + 4 条追加消息）。
    assert state.summary_offset == 13

    # 3) 活动窗口内容和索引精确匹配预期：
    #    [Summary2, R5Q, R5A, R6Q, R6A]
    #    indices: [13, 9, 10, 11, 12]
    messages, indices = await session_store.list_active_main_messages_with_indices("session-repeat")
    assert [m.content for m in messages] == [
        "<context_summary>压缩摘要</context_summary>",
        "第五轮问题",
        "第五轮回答",
        "第六轮问题",
        "第六轮回答",
    ]
    assert indices == [13, 9, 10, 11, 12]


def test_prepare_context_snapshot_raises_when_indices_empty_but_history_not() -> None:
    """history_indices 为空列表且 history 非空时，应报错而非误退化为本地索引。"""
    history = [
        StoredMessage.create(role="user", content="测试消息", timestamp=datetime(2026, 5, 6, 10, 0, 0, tzinfo=timezone.utc)),
    ]
    with pytest.raises(ValueError, match="history_indices 长度与 history 不一致"):
        ContextBuilder.prepare_context_snapshot(
            system_message=StoredMessage.create(role="system", content="系统", timestamp=datetime(2026, 5, 6, 10, 0, 0, tzinfo=timezone.utc)),
            extra_system_messages=None,
            history=history,
            history_indices=[],  # 空列表，不能退化
            current_user_message=None,
        )


def test_prepare_context_snapshot_raises_when_indices_length_mismatch() -> None:
    """history_indices 与 history 长度不一致时，应报错避免静默截断。"""
    history = [
        StoredMessage.create(role="user", content="消息1", timestamp=datetime(2026, 5, 6, 10, 0, 0, tzinfo=timezone.utc)),
        StoredMessage.create(role="assistant", content="消息2", timestamp=datetime(2026, 5, 6, 10, 0, 1, tzinfo=timezone.utc)),
    ]
    with pytest.raises(ValueError, match="history_indices 长度与 history 不一致"):
        ContextBuilder.prepare_context_snapshot(
            system_message=StoredMessage.create(role="system", content="系统", timestamp=datetime(2026, 5, 6, 10, 0, 0, tzinfo=timezone.utc)),
            extra_system_messages=None,
            history=history,
            history_indices=[0],  # 长度 1 ≠ 2
            current_user_message=None,
        )


def test_prepare_context_snapshot_accepts_none_indices() -> None:
    """history_indices=None 时正常退化为 range(len(history))，不报错。"""
    history = [
        StoredMessage.create(role="user", content="消息", timestamp=datetime(2026, 5, 6, 10, 0, 0, tzinfo=timezone.utc)),
    ]
    snapshot = ContextBuilder.prepare_context_snapshot(
        system_message=StoredMessage.create(role="system", content="系统", timestamp=datetime(2026, 5, 6, 10, 0, 0, tzinfo=timezone.utc)),
        extra_system_messages=None,
        history=history,
        history_indices=None,
        current_user_message=None,
    )
    assert len(snapshot.records) == 2  # system + 1 history
