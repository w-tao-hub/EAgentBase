"""RedisRunStore 的单元测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.models.run import ExecutionMode, Run, RunStatus, RunType
from app.core.models.error import ErrorCode
from app.infra.store.redis_run_store import RedisRunStore


@pytest.mark.asyncio  # 标记为异步测试
async def test_run_store_create_and_get_run(fake_redis):
    """测试：创建 Run 后能正确读取。"""
    store = RedisRunStore(fake_redis, key_prefix="test")  # 创建 RunStore 实例
    run = Run(  # 构造 Run 实例
        run_id="run-1",
        session_id="session-1",
        status=RunStatus.RUNNING,
        created_at=datetime(2026, 4, 3, 12, 0, 0, tzinfo=timezone.utc),
    )
    await store.create_run(run)  # 创建 Run
    retrieved = await store.get_run("run-1")  # 读取 Run
    assert retrieved is not None  # 断言读取成功
    assert retrieved.run_id == "run-1"  # 断言 run_id 正确
    assert retrieved.session_id == "session-1"  # 断言 session_id 正确
    assert retrieved.status == RunStatus.RUNNING  # 断言状态正确
    assert retrieved.run_type == RunType.MASTER  # 断言兼容默认的 master 类型
    assert retrieved.execution_mode == ExecutionMode.FOREGROUND  # 断言兼容默认前台模式
    assert retrieved.updated_at == run.created_at  # 断言缺省 updated_at 已回填为 created_at


@pytest.mark.asyncio  # 标记为异步测试
async def test_run_store_get_nonexistent_run_returns_none(fake_redis):
    """测试：读取不存在的 Run 返回 None。"""
    store = RedisRunStore(fake_redis, key_prefix="test")  # 创建 RunStore 实例
    result = await store.get_run("nonexistent")  # 读取不存在的 Run
    assert result is None  # 断言返回 None


@pytest.mark.asyncio  # 标记为异步测试
async def test_run_store_update_run_status(fake_redis):
    """测试：更新 Run 状态后能正确读取。"""
    store = RedisRunStore(fake_redis, key_prefix="test")  # 创建 RunStore 实例
    run = Run(  # 构造 Run 实例
        run_id="run-1",
        session_id="session-1",
        status=RunStatus.RUNNING,
        created_at=datetime(2026, 4, 3, 12, 0, 0, tzinfo=timezone.utc),
    )
    await store.create_run(run)  # 创建 Run
    # 更新为 completed 状态
    completed_run = Run(  # 构造 completed Run 实例
        run_id="run-1",
        session_id="session-1",
        status=RunStatus.COMPLETED,
        created_at=run.created_at,
        finished_at=datetime(2026, 4, 3, 12, 1, 0, tzinfo=timezone.utc),
        output="final answer",
    )
    await store.update_run(completed_run)  # 更新 Run
    retrieved = await store.get_run("run-1")  # 读取 Run
    assert retrieved is not None  # 断言读取成功
    assert retrieved.status == RunStatus.COMPLETED  # 断言状态已更新
    assert retrieved.finished_at is not None  # 断言 finished_at 已设置
    assert retrieved.output == "final answer"  # 断言 output 已设置


@pytest.mark.asyncio  # 标记为异步测试
async def test_run_store_update_run_to_failed(fake_redis):
    """测试：更新 Run 为失败状态后能正确读取。"""
    store = RedisRunStore(fake_redis, key_prefix="test")  # 创建 RunStore 实例
    run = Run(  # 构造 Run 实例
        run_id="run-1",
        session_id="session-1",
        status=RunStatus.RUNNING,
        created_at=datetime(2026, 4, 3, 12, 0, 0, tzinfo=timezone.utc),
    )
    await store.create_run(run)  # 创建 Run
    # 更新为 failed 状态
    failed_run = Run(  # 构造 failed Run 实例
        run_id="run-1",
        session_id="session-1",
        status=RunStatus.FAILED,
        created_at=run.created_at,
        finished_at=datetime(2026, 4, 3, 12, 1, 0, tzinfo=timezone.utc),
        error_code=ErrorCode.LLM_REQUEST_FAILED,
        error_message="Connection timeout",
    )
    await store.update_run(failed_run)  # 更新 Run
    retrieved = await store.get_run("run-1")  # 读取 Run
    assert retrieved is not None  # 断言读取成功
    assert retrieved.status == RunStatus.FAILED  # 断言状态已更新
    assert retrieved.error_code == ErrorCode.LLM_REQUEST_FAILED  # 断言错误码正确
    assert retrieved.error_message == "Connection timeout"  # 断言错误消息正确


@pytest.mark.asyncio  # 标记为异步测试
async def test_run_store_key_prefix_isolation(fake_redis):
    """测试：不同 key_prefix 的 store 使用独立的命名空间。"""
    store_a = RedisRunStore(fake_redis, key_prefix="prefix-a")  # 创建 store A
    store_b = RedisRunStore(fake_redis, key_prefix="prefix-b")  # 创建 store B
    run = Run(  # 构造 Run 实例
        run_id="run-1",
        session_id="session-1",
        status=RunStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
    )
    await store_a.create_run(run)  # 在 A 中创建 Run
    # 在 B 中应该读取不到
    result = await store_b.get_run("run-1")  # 在 B 中读取 Run
    assert result is None  # 断言返回 None


@pytest.mark.asyncio  # 标记为异步测试
async def test_run_store_datetime_serialization(fake_redis):
    """测试：datetime 字段能正确序列化和反序列化。"""
    store = RedisRunStore(fake_redis, key_prefix="test")  # 创建 RunStore 实例
    created_at = datetime(2026, 4, 3, 12, 0, 0, tzinfo=timezone.utc)  # 创建时间
    finished_at = datetime(2026, 4, 3, 12, 1, 30, tzinfo=timezone.utc)  # 完成时间
    run = Run(  # 构造 Run 实例
        run_id="run-1",
        session_id="session-1",
        status=RunStatus.COMPLETED,
        created_at=created_at,
        finished_at=finished_at,
        output="result",
    )
    await store.create_run(run)  # 创建 Run
    retrieved = await store.get_run("run-1")  # 读取 Run
    assert retrieved is not None  # 断言读取成功
    assert retrieved.created_at == created_at  # 断言创建时间正确
    assert retrieved.finished_at == finished_at  # 断言完成时间正确


@pytest.mark.asyncio  # 标记为异步测试
async def test_run_store_create_duplicate_run_raises_error(fake_redis):
    """测试：创建重复的 Run 时抛出 ValueError。"""
    store = RedisRunStore(fake_redis, key_prefix="test")  # 创建 RunStore 实例
    run = Run(  # 构造 Run 实例
        run_id="run-1",
        session_id="session-1",
        status=RunStatus.RUNNING,
        created_at=datetime(2026, 4, 3, 12, 0, 0, tzinfo=timezone.utc),
    )
    await store.create_run(run)  # 第一次创建成功
    # 尝试用相同的 run_id 再次创建（使用 RUNNING 状态避免 finished_at 验证）
    duplicate_run = Run(  # 构造重复 run_id 的 Run 实例
        run_id="run-1",  # 相同的 run_id
        session_id="session-2",  # 不同的 session_id
        status=RunStatus.RUNNING,  # 使用 RUNNING 状态避免 finished_at 验证
        created_at=datetime(2026, 4, 3, 12, 1, 0, tzinfo=timezone.utc),
    )
    with pytest.raises(ValueError, match="Run run-1 already exists"):  # 预期抛出异常
        await store.create_run(duplicate_run)  # 第二次创建应该失败


@pytest.mark.asyncio  # 标记为异步测试
async def test_run_store_round_trips_child_run_extended_fields(fake_redis):
    """测试：child run 的扩展字段能完整序列化和反序列化。"""
    store = RedisRunStore(fake_redis, key_prefix="test")  # 创建 RunStore 实例
    created_at = datetime(2026, 4, 30, 9, 0, 0, tzinfo=timezone.utc)  # child 创建时间
    updated_at = datetime(2026, 4, 30, 9, 5, 0, tzinfo=timezone.utc)  # child 更新时间
    run = Run(
        run_id="run-child-1",
        session_id="session-1",
        agent_id="child-agent",
        run_type=RunType.CHILD,
        parent_run_id="run-master-1",
        child_id="writer-1",
        tool_call_id="call-1",
        execution_mode=ExecutionMode.BACKGROUND,
        status=RunStatus.COMPLETED,
        created_at=created_at,
        updated_at=updated_at,
        finished_at=updated_at,
        output="child done",
        metadata={"trace_id": "trace-1"},
    )

    await store.create_run(run)
    retrieved = await store.get_run("run-child-1")

    assert retrieved is not None
    assert retrieved.agent_id == "child-agent"
    assert retrieved.run_type == RunType.CHILD
    assert retrieved.parent_run_id == "run-master-1"
    assert retrieved.child_id == "writer-1"
    assert retrieved.tool_call_id == "call-1"
    assert retrieved.execution_mode == ExecutionMode.BACKGROUND
    assert retrieved.updated_at == updated_at
    assert retrieved.metadata == {"trace_id": "trace-1"}


@pytest.mark.asyncio  # 标记为异步测试
async def test_run_store_get_run_compatibly_fills_legacy_defaults(fake_redis):
    """测试：旧 Run 存储数据缺少新字段时仍能按默认值恢复。"""
    run_key = "test:run:legacy-run-1"  # 构造 legacy run 的 Redis key
    await fake_redis.hset(
        run_key,
        mapping={
            "run_id": "legacy-run-1",
            "session_id": "session-legacy",
            "status": "running",
            "created_at": "2026-04-30T08:00:00+00:00",
        },
    )

    store = RedisRunStore(fake_redis, key_prefix="test")
    retrieved = await store.get_run("legacy-run-1")

    assert retrieved is not None
    assert retrieved.run_type == RunType.MASTER
    assert retrieved.execution_mode == ExecutionMode.FOREGROUND
    assert retrieved.updated_at == datetime(2026, 4, 30, 8, 0, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio  # 标记为异步测试
async def test_run_store_update_run_fields_refreshes_updated_at(fake_redis):
    """测试：部分更新终态字段时，会同步刷新 updated_at。"""
    store = RedisRunStore(fake_redis, key_prefix="test")
    created_at = datetime(2026, 4, 30, 10, 0, 0, tzinfo=timezone.utc)
    finished_at = datetime(2026, 4, 30, 10, 3, 0, tzinfo=timezone.utc)
    run = Run(
        run_id="run-1",
        session_id="session-1",
        status=RunStatus.RUNNING,
        created_at=created_at,
    )

    await store.create_run(run)
    await store.update_run_fields(
        run_id="run-1",
        status=RunStatus.COMPLETED,
        finished_at=finished_at,
        output="done",
    )
    retrieved = await store.get_run("run-1")

    assert retrieved is not None
    assert retrieved.status == RunStatus.COMPLETED
    assert retrieved.finished_at == finished_at
    assert retrieved.updated_at == finished_at
    assert retrieved.output == "done"


@pytest.mark.asyncio  # 标记为异步测试
async def test_run_store_queue_create_run_sets_ttl_when_provided(fake_redis):
    """测试：pipeline 建档在传入 ttl_seconds 时会同时设置过期时间。"""
    store = RedisRunStore(fake_redis, key_prefix="test")
    run = Run(
        run_id="run-pipeline-ttl",
        session_id="session-1",
        status=RunStatus.RUNNING,
        created_at=datetime(2026, 4, 30, 10, 0, 0, tzinfo=timezone.utc),
    )

    pipeline = fake_redis.pipeline()
    store.queue_create_run(pipeline, run, ttl_seconds=120)
    await pipeline.execute()

    ttl = await fake_redis.ttl("test:run:run-pipeline-ttl")
    assert ttl > 0
    assert ttl <= 120
