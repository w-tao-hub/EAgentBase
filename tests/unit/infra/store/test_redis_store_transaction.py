"""RedisStoreTransaction 单元测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.models.error import ErrorCode
from app.core.models.run import Run, RunStatus, RunType
from app.core.models.stored_message import StoredMessage
from app.core.ports.transactions import (
    ChildContextStartWrite,
    ChildRunTerminalWrite,
    MainRunTerminalWrite,
    RunCreateWrite,
)
from app.infra.store.redis_run_store import RedisRunStore
from app.infra.store.redis_session_store import RedisSessionStore
from app.infra.store.redis_store_transaction import RedisStoreTransaction


@pytest.fixture
def stores(fake_redis):
    """创建 Redis store 和事务适配器。"""
    session_store = RedisSessionStore(fake_redis, key_prefix="test")
    run_store = RedisRunStore(fake_redis, key_prefix="test")
    transaction = RedisStoreTransaction(
        redis=fake_redis,
        session_store=session_store,
        run_store=run_store,
    )
    return session_store, run_store, transaction


@pytest.mark.asyncio
async def test_create_run_and_index_session_writes_run_and_session_index(stores, fake_redis):
    """测试创建 Run 与 session 索引写入由事务适配器统一完成。"""
    session_store, run_store, transaction = stores
    created_at = datetime.now(timezone.utc)
    run = Run(
        run_id="run-1",
        session_id="session-1",
        status=RunStatus.RUNNING,
        created_at=created_at,
        updated_at=created_at,
    )

    await transaction.create_run_and_index_session(
        RunCreateWrite(session_id="session-1", run=run, run_ttl_seconds=60)
    )

    persisted_run = await run_store.get_run("run-1")
    run_ids = await session_store.list_session_run_ids("session-1")
    ttl = await fake_redis.ttl("test:run:run-1")

    assert persisted_run is not None
    assert persisted_run.status == RunStatus.RUNNING
    assert run_ids == ["run-1"]
    assert ttl > 0


@pytest.mark.asyncio
async def test_persist_main_run_terminal_updates_run_and_appends_message(stores):
    """测试主 Run 终态与主上下文消息会在事务适配器中合并写入。"""
    session_store, run_store, transaction = stores
    created_at = datetime.now(timezone.utc)
    run = Run(
        run_id="run-main",
        session_id="session-1",
        status=RunStatus.RUNNING,
        created_at=created_at,
        updated_at=created_at,
    )
    await run_store.create_run(run)
    message = StoredMessage.create(
        role="assistant",
        content="完成输出",
        timestamp=created_at,
    )

    await transaction.persist_main_run_terminal(
        MainRunTerminalWrite(
            session_id="session-1",
            run_id="run-main",
            status=RunStatus.COMPLETED,
            finished_at=created_at,
            output="完成输出",
            terminal_message=message,
        )
    )

    persisted_run = await run_store.get_run("run-main")
    messages = await session_store.list_main_messages("session-1")

    assert persisted_run is not None
    assert persisted_run.status == RunStatus.COMPLETED
    assert persisted_run.output == "完成输出"
    assert messages[-1].role == "assistant"
    assert messages[-1].content == "完成输出"


@pytest.mark.asyncio
async def test_append_child_input_and_summary_writes_context_and_summary(stores):
    """测试 child 首条输入消息和可恢复摘要会合并写入。"""
    session_store, _run_store, transaction = stores
    message = StoredMessage.create(
        role="user",
        content="请分析代码",
        timestamp=datetime.now(timezone.utc),
        child_id="child-1",
        subagent_type="Worker",
    )

    await transaction.append_child_input_and_summary(
        ChildContextStartWrite(
            session_id="session-1",
            child_id="child-1",
            child_run_id="child-run-1",
            user_message=message,
            subagent_type="Worker",
            description="分析代码",
        )
    )

    messages = await session_store.list_child_messages("session-1", "child-1")
    summaries = await session_store.list_session_child_summaries("session-1")

    assert messages[0].content == "请分析代码"
    assert messages[0].meta.source_run_id == "child-run-1"
    assert summaries[0].resume_id == "child-1"
    assert summaries[0].description == "分析代码"


@pytest.mark.asyncio
async def test_persist_child_run_terminal_updates_run_and_optional_message(stores):
    """测试 child 终态写入支持可选 child 上下文提示消息。"""
    session_store, run_store, transaction = stores
    created_at = datetime.now(timezone.utc)
    run = Run(
        run_id="child-run-1",
        session_id="session-1",
        child_id="child-1",
        parent_run_id="run-main",
        tool_call_id="tool-call-1",
        run_type=RunType.CHILD,
        status=RunStatus.RUNNING,
        created_at=created_at,
        updated_at=created_at,
    )
    await run_store.create_run(run)
    message = StoredMessage.create(
        role="system",
        content="此次生成已被用户取消。",
        timestamp=created_at,
        is_meta=True,
        subagent_type="Worker",
    )

    await transaction.persist_child_run_terminal(
        ChildRunTerminalWrite(
            session_id="session-1",
            child_id="child-1",
            child_run_id="child-run-1",
            status=RunStatus.CANCELLED,
            finished_at=created_at,
            subagent_type="Worker",
            error_code=ErrorCode.RUN_CANCELLED,
            error_message="cancelled",
            terminal_message=message,
        )
    )

    persisted_run = await run_store.get_run("child-run-1")
    messages = await session_store.list_child_messages("session-1", "child-1")

    assert persisted_run is not None
    assert persisted_run.status == RunStatus.CANCELLED
    assert persisted_run.error_code == ErrorCode.RUN_CANCELLED
    assert messages[-1].role == "system"
    assert messages[-1].is_meta is True
