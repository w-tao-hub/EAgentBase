"""RedisSessionStore 的单元测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import pytest

from app.core.models.session import Session
from app.core.models.stored_message import StoredMessage
from app.infra.store.redis_session_store import ContextSummaryState, RedisSessionStore


@pytest.mark.asyncio  # 标记为异步测试
async def test_session_store_create_and_get_session(fake_redis):
    """测试：创建会话后能正确读取。"""
    store = RedisSessionStore(fake_redis, key_prefix="test")  # 创建 SessionStore 实例
    session = Session(  # 构造 Session 实例
        session_id="session-1",
        agent_id="agent-1",
        created_at=datetime(2026, 4, 3, 12, 0, 0, tzinfo=timezone.utc),
    )
    await store.create_session(session)  # 创建会话
    retrieved = await store.get_session("session-1")  # 读取会话
    assert retrieved is not None  # 断言读取成功
    assert retrieved.session_id == "session-1"  # 断言 session_id 正确
    assert retrieved.agent_id == "agent-1"  # 断言 agent_id 正确
    assert retrieved.created_at == session.created_at  # 断言创建时间正确


@pytest.mark.asyncio  # 标记为异步测试
async def test_session_store_get_nonexistent_session_returns_none(fake_redis):
    """测试：读取不存在的会话返回 None。"""
    store = RedisSessionStore(fake_redis, key_prefix="test")  # 创建 SessionStore 实例
    result = await store.get_session("nonexistent")  # 读取不存在的会话
    assert result is None  # 断言返回 None


@pytest.mark.asyncio  # 标记为异步测试
async def test_session_store_get_message_count_empty(fake_redis):
    """测试：空会话的消息数量为 0。"""
    store = RedisSessionStore(fake_redis, key_prefix="test")  # 创建 SessionStore 实例
    session = Session(  # 构造 Session 实例
        session_id="session-1",
        agent_id="agent-1",
        created_at=datetime.now(timezone.utc),
    )
    await store.create_session(session)  # 创建会话
    count = await store.get_message_count("session-1")  # 获取消息数量
    assert count == 0  # 断言消息数量为 0


@pytest.mark.asyncio  # 标记为异步测试
async def test_session_store_append_and_list_messages(fake_redis):
    """测试：追加消息后能正确列出。"""
    store = RedisSessionStore(fake_redis, key_prefix="test")  # 创建 SessionStore 实例
    session = Session(  # 构造 Session 实例
        session_id="session-1",
        agent_id="agent-1",
        created_at=datetime.now(timezone.utc),
    )
    await store.create_session(session)  # 创建会话
    message = StoredMessage.create(  # 构造消息实例
        role="user",
        content="hello",
        timestamp=datetime(2026, 4, 3, 12, 0, 0, tzinfo=timezone.utc),
    )
    await store.append_message("session-1", message)  # 追加消息
    messages = await store.list_messages("session-1")  # 列出消息
    assert len(messages) == 1  # 断言消息数量为 1
    assert messages[0].role == "user"  # 断言角色正确
    assert messages[0].content == "hello"  # 断言内容正确


@pytest.mark.asyncio
async def test_session_store_main_context_api_append_and_list_messages(fake_redis):
    """测试：显式主会话上下文 API 会写入新的主上下文 key。"""
    store = RedisSessionStore(fake_redis, key_prefix="test")
    session = Session(
        session_id="session-main",
        agent_id="agent-1",
        created_at=datetime.now(timezone.utc),
    )
    await store.create_session(session)

    message = StoredMessage.create(
        role="assistant",
        content="主会话上下文消息",
        timestamp=datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc),
    )

    await store.append_main_message("session-main", message, source_run_id="run-main-1")

    messages = await store.list_main_messages("session-main")
    raw_messages = await fake_redis.lrange(store._session_main_messages_key("session-main"), 0, -1)

    assert len(messages) == 1
    assert messages[0].content == "主会话上下文消息"
    assert len(raw_messages) == 1
    assert '"source_run_id": "run-main-1"' in raw_messages[0]
    assert '"_meta"' in raw_messages[0]


@pytest.mark.asyncio  # 标记为异步测试
async def test_session_store_get_message_count_after_append(fake_redis):
    """测试：追加消息后消息数量正确。"""
    store = RedisSessionStore(fake_redis, key_prefix="test")  # 创建 SessionStore 实例
    session = Session(  # 构造 Session 实例
        session_id="session-1",
        agent_id="agent-1",
        created_at=datetime.now(timezone.utc),
    )
    await store.create_session(session)  # 创建会话
    message1 = StoredMessage.create(  # 构造第一条消息
        role="user",
        content="hello",
        timestamp=datetime.now(timezone.utc),
    )
    message2 = StoredMessage.create(  # 构造第二条消息
        role="assistant",
        content="hi there",
        timestamp=datetime.now(timezone.utc),
    )
    await store.append_message("session-1", message1)  # 追加第一条消息
    await store.append_message("session-1", message2)  # 追加第二条消息
    count = await store.get_message_count("session-1")  # 获取消息数量
    assert count == 2  # 断言消息数量为 2


@pytest.mark.asyncio  # 标记为异步测试
async def test_session_store_list_messages_order(fake_redis):
    """测试：消息按追加顺序返回。"""
    store = RedisSessionStore(fake_redis, key_prefix="test")  # 创建 SessionStore 实例
    session = Session(  # 构造 Session 实例
        session_id="session-1",
        agent_id="agent-1",
        created_at=datetime.now(timezone.utc),
    )
    await store.create_session(session)  # 创建会话
    # 按顺序追加多条消息
    for i in range(3):
        message = StoredMessage.create(  # 构造消息实例
            role="user",
            content=f"message-{i}",
            timestamp=datetime(2026, 4, 3, 12, i, 0, tzinfo=timezone.utc),
        )
        await store.append_message("session-1", message)  # 追加消息
    messages = await store.list_messages("session-1")  # 列出消息
    assert len(messages) == 3  # 断言消息数量为 3
    for i, msg in enumerate(messages):
        assert msg.content == f"message-{i}"  # 断言顺序正确


@pytest.mark.asyncio  # 标记为异步测试
async def test_session_store_list_messages_supports_range(fake_redis):
    """测试：list_messages 支持按 LRANGE 偏移范围读取尾段消息。"""
    store = RedisSessionStore(fake_redis, key_prefix="test")  # 创建 SessionStore 实例
    session = Session(  # 构造 Session 实例
        session_id="session-range",
        agent_id="agent-1",
        created_at=datetime.now(timezone.utc),
    )
    await store.create_session(session)  # 先创建会话元数据

    for index in range(5):  # 连续写入五条消息，便于后续验证范围读取。
        await store.append_message(
            "session-range",
            StoredMessage.create(
                role="user",
                content=f"message-{index}",
                timestamp=datetime(2026, 4, 13, 12, index, 0, tzinfo=timezone.utc),
            ),
        )

    messages = await store.list_messages("session-range", start=2, end=3)  # 仅读取中间两条消息，验证 start/end 语义。

    assert [message.content for message in messages] == ["message-2", "message-3"]  # 范围读取应保持原始顺序且仅返回目标片段。


@pytest.mark.asyncio  # 标记为异步测试
async def test_session_store_key_prefix_isolation(fake_redis):
    """测试：不同 key_prefix 的 store 使用独立的命名空间。"""
    store_a = RedisSessionStore(fake_redis, key_prefix="prefix-a")  # 创建 store A
    store_b = RedisSessionStore(fake_redis, key_prefix="prefix-b")  # 创建 store B
    session = Session(  # 构造 Session 实例
        session_id="session-1",
        agent_id="agent-1",
        created_at=datetime.now(timezone.utc),
    )
    await store_a.create_session(session)  # 在 A 中创建会话
    # 在 B 中应该读取不到
    result = await store_b.get_session("session-1")  # 在 B 中读取会话
    assert result is None  # 断言返回 None


@pytest.mark.asyncio
async def test_session_store_preserves_is_meta_flag(fake_redis):
    """测试 RedisSessionStore 会持久化并恢复消息的 isMeta 标记。"""
    store = RedisSessionStore(fake_redis, key_prefix="test")
    session = Session(
        session_id="session-1",
        agent_id="agent-1",
        created_at=datetime.now(timezone.utc),
    )
    await store.create_session(session)

    await store.append_message(
        "session-1",
        StoredMessage.create(
            role="user",
            content="hidden skill",
            timestamp=datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc),
            is_meta=True,
        ),
    )

    messages = await store.list_messages("session-1")

    assert len(messages) == 1
    assert messages[0].is_meta is True


@pytest.mark.asyncio
async def test_session_store_can_mark_history_dirty(fake_redis):
    """测试会话历史 dirty 标记可以正确写入与读取。"""
    store = RedisSessionStore(fake_redis, key_prefix="test")

    assert await store.is_history_dirty("session-1") is False

    await store.mark_history_dirty("session-1")

    assert await store.is_history_dirty("session-1") is True


@pytest.mark.asyncio
async def test_session_store_can_manage_session_runs_and_children(fake_redis):
    """测试：session_runs 与 session_children 索引可以独立维护。"""
    store = RedisSessionStore(fake_redis, key_prefix="test")
    session = Session(
        session_id="session-index",
        agent_id="agent-1",
        created_at=datetime.now(timezone.utc),
    )
    await store.create_session(session)

    await store.add_session_run(
        "session-index",
        "run-2",
        created_at=datetime(2026, 4, 30, 12, 3, 2, tzinfo=timezone.utc),
    )
    await store.add_session_run(
        "session-index",
        "run-1",
        created_at=datetime(2026, 4, 30, 12, 3, 1, tzinfo=timezone.utc),
    )
    await store.upsert_session_child_summary("session-index", "child-b", subagent_type="", description="")
    await store.upsert_session_child_summary("session-index", "child-a", subagent_type="", description="")
    await store.upsert_session_child_summary("session-index", "child-a", subagent_type="", description="")

    assert await store.list_session_runs("session-index") == ["run-1", "run-2"]
    assert await store.list_session_run_ids("session-index") == ["run-1", "run-2"]
    assert await store.list_session_children("session-index") == ["child-a", "child-b"]
    assert await store.list_session_child_ids("session-index") == ["child-a", "child-b"]


@pytest.mark.asyncio
async def test_session_store_can_upsert_session_child_summaries(fake_redis):
    """测试 session_children 会以 HASH 形式保存 child 摘要，并返回稳定排序。"""
    store = RedisSessionStore(fake_redis, key_prefix="test")
    session = Session(
        session_id="session-index",
        agent_id="agent-1",
        created_at=datetime.now(timezone.utc),
    )
    await store.create_session(session)

    await store.upsert_session_child_summary(
        "session-index",
        child_id="child-b",
        subagent_type="Plan",
        description="第二个子代理",
    )
    await store.upsert_session_child_summary(
        "session-index",
        child_id="child-a",
        subagent_type="Worker",
        description="第一个子代理",
    )

    summaries = await store.list_session_child_summaries("session-index")

    assert [summary.resume_id for summary in summaries] == ["child-a", "child-b"]
    assert summaries[0].subagent_type == "Worker"
    assert summaries[0].description == "第一个子代理"
    assert summaries[1].subagent_type == "Plan"
    assert summaries[1].description == "第二个子代理"
    assert await store.list_session_child_ids("session-index") == ["child-a", "child-b"]


@pytest.mark.asyncio
async def test_session_store_resume_updates_description_without_creating_duplicate_child(fake_redis):
    """测试同一 child 重复 upsert 时只覆盖 description，不会新增重复项。"""
    store = RedisSessionStore(fake_redis, key_prefix="test")
    session = Session(
        session_id="session-resume",
        agent_id="agent-1",
        created_at=datetime.now(timezone.utc),
    )
    await store.create_session(session)

    await store.upsert_session_child_summary(
        "session-resume",
        child_id="plan-resume",
        subagent_type="Plan",
        description="第一次描述",
    )
    await store.upsert_session_child_summary(
        "session-resume",
        child_id="plan-resume",
        subagent_type="Plan",
        description="第二次描述",
    )

    summaries = await store.list_session_child_summaries("session-resume")

    assert len(summaries) == 1
    assert summaries[0].resume_id == "plan-resume"
    assert summaries[0].subagent_type == "Plan"
    assert summaries[0].description == "第二次描述"


@pytest.mark.asyncio
async def test_session_store_can_ensure_session_child_registered_without_wiping_summary(fake_redis):
    """测试轻量登记 child 不会把已有 description 覆盖为空。"""
    store = RedisSessionStore(fake_redis, key_prefix="test")
    session = Session(
        session_id="session-ensure",
        agent_id="agent-1",
        created_at=datetime.now(timezone.utc),
    )
    await store.create_session(session)

    await store.upsert_session_child_summary(
        "session-ensure",
        child_id="plan-ensure",
        subagent_type="Plan",
        description="已有描述",
    )
    await store.ensure_session_child_registered("session-ensure", "plan-ensure")

    summaries = await store.list_session_child_summaries("session-ensure")

    assert len(summaries) == 1
    assert summaries[0].description == "已有描述"


@pytest.mark.asyncio
async def test_session_store_can_append_context_summary_and_restore_active_messages(fake_redis):
    """测试会话存储可以记录最近一次上下文摘要边界并恢复活动窗口。"""
    store = RedisSessionStore(fake_redis, key_prefix="test")
    session = Session(
        session_id="session-1",
        agent_id="agent-1",
        created_at=datetime.now(timezone.utc),
    )
    await store.create_session(session)

    await store.append_message(
        "session-1",
        StoredMessage.create(
            role="user",
            content="第一轮问题",
            timestamp=datetime(2026, 4, 11, 10, 0, 0, tzinfo=timezone.utc),
        ),
    )
    await store.append_message(
        "session-1",
        StoredMessage.create(
            role="assistant",
            content="第一轮回答",
            timestamp=datetime(2026, 4, 11, 10, 0, 1, tzinfo=timezone.utc),
        ),
    )
    await store.append_message(
        "session-1",
        StoredMessage.create(
            role="user",
            content="第二轮问题",
            timestamp=datetime(2026, 4, 11, 10, 1, 0, tzinfo=timezone.utc),
        ),
    )
    await store.append_message(
        "session-1",
        StoredMessage.create(
            role="assistant",
            content="第二轮回答",
            timestamp=datetime(2026, 4, 11, 10, 1, 1, tzinfo=timezone.utc),
        ),
    )

    summary_message = StoredMessage.create(
        role="user",
        content="<context_summary>压缩后的旧历史</context_summary>",
        timestamp=datetime(2026, 4, 11, 10, 2, 0, tzinfo=timezone.utc),
        is_meta=True,
    )
    # 获取活动窗口起点消息（第二轮问题）
    messages_before_summary = await store.list_messages("session-1")
    active_start_message = messages_before_summary[2]

    state = await store.append_context_summary(
        "session-1",
        summary_message,
        active_start_message=active_start_message,
    )

    assert state.summary_message_id == summary_message.message_id
    assert state.active_start_message_id == active_start_message.message_id
    assert state.summary_offset == 4
    assert state.active_start_offset == 2

    persisted_state = await store.get_context_summary_state("session-1")

    assert persisted_state == ContextSummaryState(
        summary_message_id=summary_message.message_id,
        active_start_message_id=active_start_message.message_id,
        summary_offset=4,
        active_start_offset=2,
    )

    active_messages = await store.list_active_messages("session-1")

    assert [message.content for message in active_messages] == [
        "<context_summary>压缩后的旧历史</context_summary>",
        "第二轮问题",
        "第二轮回答",
    ]


@pytest.mark.asyncio
async def test_session_store_append_context_summary_uses_single_pipeline_execute(fake_redis, monkeypatch):
    """测试摘要消息与摘要边界状态会合并到一次 pipeline.execute()。"""
    execute_calls = 0  # 记录 pipeline.execute 调用次数，验证 RPUSH 与 SET 会一起发送。
    original_pipeline = fake_redis.pipeline  # 保存原始 pipeline 工厂，继续复用 fakeredis 的真实行为。
    store = RedisSessionStore(fake_redis, key_prefix="test")  # 创建被测 store。

    class RecordingPipeline:
        """包装真实 pipeline，并记录 execute 调用次数。"""

        def __init__(self, inner) -> None:
            """保存被包装的真实 pipeline。"""
            self._inner = inner

        def rpush(self, *args, **kwargs):
            """透传 RPUSH 命令到真实 pipeline。"""
            self._inner.rpush(*args, **kwargs)
            return self

        def set(self, *args, **kwargs):
            """透传 SET 命令到真实 pipeline。"""
            self._inner.set(*args, **kwargs)
            return self

        async def execute(self):
            """记录 execute 次数后执行真实 pipeline。"""
            nonlocal execute_calls
            execute_calls += 1
            return await self._inner.execute()

    monkeypatch.setattr(fake_redis, "pipeline", lambda: RecordingPipeline(original_pipeline()))

    session = Session(
        session_id="session-pipeline",
        agent_id="agent-1",
        created_at=datetime.now(timezone.utc),
    )
    await store.create_session(session)

    history_messages = [
        StoredMessage.create(role="user", content="第一轮问题", timestamp=datetime(2026, 4, 13, 9, 0, 0, tzinfo=timezone.utc)),
        StoredMessage.create(role="assistant", content="第一轮回答", timestamp=datetime(2026, 4, 13, 9, 0, 1, tzinfo=timezone.utc)),
        StoredMessage.create(role="user", content="第二轮问题", timestamp=datetime(2026, 4, 13, 9, 1, 0, tzinfo=timezone.utc)),
        StoredMessage.create(role="assistant", content="第二轮回答", timestamp=datetime(2026, 4, 13, 9, 1, 1, tzinfo=timezone.utc)),
    ]
    for message in history_messages:
        await store.append_message("session-pipeline", message)

    summary_message = StoredMessage.create(
        role="user",
        content="<context_summary>摘要</context_summary>",
        timestamp=datetime(2026, 4, 13, 9, 2, 0, tzinfo=timezone.utc),
        is_meta=True,
    )

    state = await store.append_context_summary(
        "session-pipeline",
        summary_message,
        active_start_message=history_messages[2],
    )

    assert execute_calls == 1  # 摘要消息写入与状态刷新应只执行一次 pipeline。
    assert state.summary_message_id == summary_message.message_id  # 返回值仍应保留摘要消息 ID。
    assert state.active_start_message_id == history_messages[2].message_id  # 返回值仍应保留活动窗口起点 ID。

    persisted_state = await store.get_context_summary_state("session-pipeline")
    active_messages = await store.list_active_messages("session-pipeline")

    assert persisted_state == state  # Redis 中持久化的状态应与返回值一致。
    assert [message.content for message in active_messages] == [
        "<context_summary>摘要</context_summary>",
        "第二轮问题",
        "第二轮回答",
    ]  # 活动窗口语义不应因 pipeline 改写而变化。


@pytest.mark.asyncio
async def test_session_store_list_active_messages_reads_history_and_summary_state_concurrently(fake_redis, monkeypatch):
    """测试无索引活动窗口读取会并行启动历史与摘要状态查询。"""
    store = RedisSessionStore(fake_redis, key_prefix="test")  # 创建被测 store。
    session = Session(
        session_id="session-concurrent",
        agent_id="agent-1",
        created_at=datetime.now(timezone.utc),
    )
    await store.create_session(session)
    await store.append_message(
        "session-concurrent",
        StoredMessage.create(
            role="user",
            content="并行读取验证",
            timestamp=datetime(2026, 4, 13, 9, 30, 0, tzinfo=timezone.utc),
        ),
    )

    original_list_messages = store.list_messages  # 保存原始消息读取方法，确保仍走真实反序列化逻辑。
    list_messages_started = asyncio.Event()  # 用于证明消息读取已在摘要状态返回前启动。

    async def recording_list_messages(session_id: str, start: int = 0, end: int = -1):
        """记录消息读取已启动，再调用真实实现。"""
        list_messages_started.set()
        return await original_list_messages(session_id, start=start, end=end)

    async def blocking_get_context_summary_state(session_id: str):
        """等待消息读取启动，若仍是串行实现则此处会超时失败。"""
        await asyncio.wait_for(list_messages_started.wait(), timeout=0.1)
        return None

    monkeypatch.setattr(store, "list_messages", recording_list_messages)
    monkeypatch.setattr(store, "get_context_summary_state", blocking_get_context_summary_state)

    active_messages = await store.list_active_messages("session-concurrent")

    assert [message.content for message in active_messages] == ["并行读取验证"]  # 无摘要状态时仍应返回完整历史。


@pytest.mark.asyncio
async def test_session_store_active_messages_fallback_when_start_message_deleted(fake_redis):
    """测试删除摘要之前的消息后，仍能按 UUID 正确定位摘要，并返回摘要及之后消息。"""
    store = RedisSessionStore(fake_redis, key_prefix="test")
    session = Session(
        session_id="session-del",
        agent_id="agent-1",
        created_at=datetime.now(timezone.utc),
    )
    await store.create_session(session)

    messages = [
        StoredMessage.create(role="user", content="第一轮", timestamp=datetime(2026, 4, 11, 10, 0, 0, tzinfo=timezone.utc)),
        StoredMessage.create(role="assistant", content="第一轮答", timestamp=datetime(2026, 4, 11, 10, 0, 1, tzinfo=timezone.utc)),
        StoredMessage.create(role="user", content="第二轮", timestamp=datetime(2026, 4, 11, 10, 1, 0, tzinfo=timezone.utc)),
        StoredMessage.create(role="assistant", content="第二轮答", timestamp=datetime(2026, 4, 11, 10, 1, 1, tzinfo=timezone.utc)),
    ]
    for message in messages:
        await store.append_message("session-del", message)

    summary_message = StoredMessage.create(
        role="user",
        content="<context_summary>摘要</context_summary>",
        timestamp=datetime(2026, 4, 11, 10, 2, 0, tzinfo=timezone.utc),
        is_meta=True,
    )
    active_start_message = messages[2]  # 第二轮
    await store.append_context_summary("session-del", summary_message, active_start_message)

    # 直接从 Redis 链表中删除前两轮消息（模拟只保留最近 N 条）
    messages_key = store._session_main_messages_key("session-del")
    await fake_redis.ltrim(messages_key, 2, -1)

    active_messages = await store.list_active_messages("session-del")

    # 起点消息被删除后，至少能正确定位摘要，并返回摘要及其后的消息
    assert active_messages[0].content == "<context_summary>摘要</context_summary>"
    assert active_messages[0].message_id == summary_message.message_id
    assert [message.content for message in active_messages[1:]] == [
        "第二轮",
        "第二轮答",
    ]


@pytest.mark.asyncio
async def test_session_store_list_active_messages_with_indices_uses_ranged_tail_window(fake_redis):
    """测试活动窗口读取会优先复用摘要状态中的偏移信息，并返回绝对索引映射。"""
    store = RedisSessionStore(fake_redis, key_prefix="test")
    session = Session(
        session_id="session-fast-path",
        agent_id="agent-1",
        created_at=datetime.now(timezone.utc),
    )
    await store.create_session(session)

    history_messages = [
        StoredMessage.create(role="user", content="第一轮问题", timestamp=datetime(2026, 4, 13, 10, 0, 0, tzinfo=timezone.utc)),
        StoredMessage.create(role="assistant", content="第一轮回答", timestamp=datetime(2026, 4, 13, 10, 0, 1, tzinfo=timezone.utc)),
        StoredMessage.create(role="user", content="第二轮问题", timestamp=datetime(2026, 4, 13, 10, 1, 0, tzinfo=timezone.utc)),
        StoredMessage.create(role="assistant", content="第二轮回答", timestamp=datetime(2026, 4, 13, 10, 1, 1, tzinfo=timezone.utc)),
        StoredMessage.create(role="user", content="第三轮问题", timestamp=datetime(2026, 4, 13, 10, 2, 0, tzinfo=timezone.utc)),
        StoredMessage.create(role="assistant", content="第三轮回答", timestamp=datetime(2026, 4, 13, 10, 2, 1, tzinfo=timezone.utc)),
    ]
    for message in history_messages:
        await store.append_message("session-fast-path", message)

    summary_message = StoredMessage.create(
        role="user",
        content="<context_summary>摘要</context_summary>",
        timestamp=datetime(2026, 4, 13, 10, 3, 0, tzinfo=timezone.utc),
        is_meta=True,
    )
    await store.append_context_summary(
        "session-fast-path",
        summary_message,
        active_start_message=history_messages[2],
        active_start_offset=2,
    )

    active_messages, active_indices = await store.list_active_messages_with_indices("session-fast-path")

    assert [message.content for message in active_messages] == [
        "<context_summary>摘要</context_summary>",
        "第二轮问题",
        "第二轮回答",
        "第三轮问题",
        "第三轮回答",
    ]
    assert active_indices == [6, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_session_store_child_context_supports_dirty_summary_and_active_window(fake_redis):
    """测试：child 长期上下文支持独立消息流、dirty 标记与摘要边界。"""
    store = RedisSessionStore(fake_redis, key_prefix="test")
    session = Session(
        session_id="session-child",
        agent_id="agent-1",
        created_at=datetime.now(timezone.utc),
    )
    await store.create_session(session)

    history_messages = [
        StoredMessage.create(role="user", content="child 第一轮问题", timestamp=datetime(2026, 4, 30, 12, 4, 0, tzinfo=timezone.utc)),
        StoredMessage.create(role="assistant", content="child 第一轮回答", timestamp=datetime(2026, 4, 30, 12, 4, 1, tzinfo=timezone.utc)),
        StoredMessage.create(role="user", content="child 第二轮问题", timestamp=datetime(2026, 4, 30, 12, 5, 0, tzinfo=timezone.utc)),
        StoredMessage.create(role="assistant", content="child 第二轮回答", timestamp=datetime(2026, 4, 30, 12, 5, 1, tzinfo=timezone.utc)),
    ]
    for message in history_messages:
        await store.append_child_message("session-child", "writer-1", message, source_run_id="run-child-1")

    assert await store.get_child_message_count("session-child", "writer-1") == 4

    await store.mark_child_history_dirty("session-child", "writer-1")
    assert await store.is_child_history_dirty("session-child", "writer-1") is True

    summary_message = StoredMessage.create(
        role="user",
        content="<context_summary>child 摘要</context_summary>",
        timestamp=datetime(2026, 4, 30, 12, 6, 0, tzinfo=timezone.utc),
        is_meta=True,
    )
    state = await store.append_child_context_summary(
        "session-child",
        "writer-1",
        summary_message,
        active_start_message=history_messages[2],
        active_start_offset=2,
    )

    active_messages = await store.list_child_active_messages("session-child", "writer-1")
    active_messages_with_indices, active_indices = await store.list_child_active_messages_with_indices("session-child", "writer-1")

    assert state == ContextSummaryState(
        summary_message_id=summary_message.message_id,
        active_start_message_id=history_messages[2].message_id,
        summary_offset=4,
        active_start_offset=2,
    )
    assert await store.get_child_context_summary_state("session-child", "writer-1") == state
    assert [message.content for message in active_messages] == [
        "<context_summary>child 摘要</context_summary>",
        "child 第二轮问题",
        "child 第二轮回答",
    ]
    assert [message.content for message in active_messages_with_indices] == [
        "<context_summary>child 摘要</context_summary>",
        "child 第二轮问题",
        "child 第二轮回答",
    ]
    assert active_indices == [4, 2, 3]
    assert await store.list_session_children("session-child") == ["writer-1"]
    raw_child_messages = await fake_redis.lrange(store._child_context_messages_key("session-child", "writer-1"), 0, -1)
    assert '"child_id": "writer-1"' in raw_child_messages[0]
    assert '"source_run_id": "run-child-1"' in raw_child_messages[0]
