"""RedisToolResultStore 单元测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.infra.store.redis_tool_result_store import RedisToolResultStore


@pytest.mark.asyncio
async def test_tool_result_store_persists_and_reads_result(fake_redis) -> None:
    """测试 store 能保存完整结果并按 session 读取。"""
    store = RedisToolResultStore(fake_redis, key_prefix="test")  # 创建隔离命名空间下的被测 store。

    key = await store.persist_result(  # 持久化一条完整结果，获取查询 key。
        session_id="session-1",
        tool_name="search",
        content="完整输出正文",
    )

    result = await store.get_result(key=key, session_id="session-1")  # 读取当前会话下的持久化结果。

    assert result is not None  # 命中时应返回记录对象。
    assert result.key == key  # 返回的 key 应与持久化时一致。
    assert result.session_id == "session-1"  # session_id 应被完整保存。
    assert result.tool_name == "search"  # tool_name 应被完整保存。
    assert result.content == "完整输出正文"  # content 应保持原文不变。
    assert result.content_length == len("完整输出正文")  # 内容长度应与原始正文一致。
    assert isinstance(result.created_at, datetime)  # created_at 应被反序列化为 datetime。
    assert result.created_at.tzinfo == timezone.utc  # 时间应保持 UTC 时区语义。


@pytest.mark.asyncio
async def test_tool_result_store_persist_result_uses_single_pipeline_execute(fake_redis, monkeypatch) -> None:
    """测试完整结果正文与 session 索引会合并到一次 pipeline.execute()。"""
    execute_calls = 0  # 记录 pipeline.execute 调用次数，验证 HSET 与 SADD 只往返一次 Redis。
    original_pipeline = fake_redis.pipeline  # 保存原始 pipeline 工厂，便于继续复用 fakeredis 的真实行为。
    store = RedisToolResultStore(fake_redis, key_prefix="test")  # 创建被测 store。

    class RecordingPipeline:
        """包装真实 pipeline，并记录 execute 调用次数。"""

        def __init__(self, inner) -> None:
            """保存被包装的真实 pipeline。"""
            self._inner = inner

        def hset(self, *args, **kwargs):
            """透传 HSET 命令到真实 pipeline。"""
            self._inner.hset(*args, **kwargs)
            return self

        def sadd(self, *args, **kwargs):
            """透传 SADD 命令到真实 pipeline。"""
            self._inner.sadd(*args, **kwargs)
            return self

        async def execute(self):
            """记录 execute 次数后执行真实 pipeline。"""
            nonlocal execute_calls
            execute_calls += 1
            return await self._inner.execute()

    monkeypatch.setattr(fake_redis, "pipeline", lambda: RecordingPipeline(original_pipeline()))

    key = await store.persist_result(
        session_id="session-pipeline",
        tool_name="search",
        content="完整输出正文",
    )

    result = await store.get_result(key=key, session_id="session-pipeline")

    assert execute_calls == 1  # 正文写入与索引建立应只执行一次 pipeline。
    assert result is not None  # 语义上仍应能正常读回。
    assert result.content == "完整输出正文"  # 正文内容不应因 pipeline 合并而变化。
    assert result.tool_name == "search"  # 工具名称应保持原样。


@pytest.mark.asyncio
async def test_tool_result_store_rejects_key_from_other_session(fake_redis) -> None:
    """测试 store 不允许跨 session 读取结果。"""
    store = RedisToolResultStore(fake_redis, key_prefix="test")  # 创建被测 store。
    key = await store.persist_result(  # 先在 session-1 下保存结果。
        session_id="session-1",
        tool_name="search",
        content="完整输出正文",
    )

    result = await store.get_result(key=key, session_id="session-2")  # 改用其他 session_id 读取同一 key。

    assert result is None  # 不属于当前会话时必须拒绝读取。


@pytest.mark.asyncio
async def test_tool_result_store_returns_none_for_invalid_key_namespace(fake_redis) -> None:
    """测试非法命名空间 key 不会被读取。"""
    store = RedisToolResultStore(fake_redis, key_prefix="test")  # 创建被测 store。

    result = await store.get_result(key="wrong:key", session_id="session-1")  # 使用非法 key 命名空间尝试读取。

    assert result is None  # 非法 key 应直接返回空。


@pytest.mark.asyncio
async def test_tool_result_store_deletes_all_results_for_session(fake_redis) -> None:
    """测试按 session 清理会删除正文与索引。"""
    store = RedisToolResultStore(fake_redis, key_prefix="test")  # 创建被测 store。
    key1 = await store.persist_result(session_id="session-1", tool_name="search", content="结果-1")  # 保存第一条结果。
    key2 = await store.persist_result(session_id="session-1", tool_name="grep", content="结果-2")  # 保存第二条结果。
    other_key = await store.persist_result(session_id="session-2", tool_name="grep", content="结果-3")  # 另一个会话的结果应保留。

    deleted_count = await store.delete_session_results("session-1")  # 清理 session-1 下的全部结果。

    assert deleted_count == 2  # 应删除该 session 下的两条正文结果。
    assert await fake_redis.exists(key1) == 0  # 第一条正文 key 应被删除。
    assert await fake_redis.exists(key2) == 0  # 第二条正文 key 应被删除。
    assert await fake_redis.exists(other_key) == 1  # 其他会话的正文 key 不应受影响。
    assert await fake_redis.exists("test:session_tool_results:session-1") == 0  # session-1 的索引集合也应被删除。
