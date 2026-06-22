"""Redis 复合写入事务适配器。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.ports.transactions import (
    ChildContextStartWrite,
    ChildRunTerminalWrite,
    MainRunTerminalWrite,
    RunCreateWrite,
)

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from app.infra.store.redis_run_store import RedisRunStore
    from app.infra.store.redis_session_store import RedisSessionStore


class RedisStoreTransaction:
    """基于 Redis pipeline 的复合写入适配器。

    该类是 Redis pipeline 的唯一默认出口之一。服务层只能依赖
    StoreTransaction 语义，不直接接触 pipeline 或 Redis 连接对象。
    """

    def __init__(
        self,
        *,
        redis: "Redis",
        session_store: "RedisSessionStore",
        run_store: "RedisRunStore",
    ) -> None:
        """保存 Redis 与具体 Store 依赖。"""
        self._redis = redis
        self._session_store = session_store
        self._run_store = run_store

    async def create_run_and_index_session(self, write: RunCreateWrite) -> None:
        """创建 Run，并写入 session 级 run 索引。"""
        pipeline = self._redis.pipeline()
        self._run_store.queue_create_run(
            pipeline,
            write.run,
            ttl_seconds=write.run_ttl_seconds,
        )
        self._session_store.queue_add_session_run(
            pipeline,
            session_id=write.session_id,
            run_id=write.run.run_id,
            created_at_ts=write.run.created_at.timestamp(),
        )
        await pipeline.execute()

    async def persist_main_run_terminal(self, write: MainRunTerminalWrite) -> None:
        """持久化主 Run 终态，并按需追加主上下文终态消息。"""
        pipeline = self._redis.pipeline()
        self._run_store.queue_update_run_fields(
            pipeline=pipeline,
            run_id=write.run_id,
            status=write.status,
            finished_at=write.finished_at,
            output=write.output,
            error_code=write.error_code,
            error_message=write.error_message,
        )
        if write.terminal_message is not None:
            self._session_store.queue_append_main_message(
                pipeline=pipeline,
                session_id=write.session_id,
                message=write.terminal_message,
                source_run_id=write.run_id,
            )
        await pipeline.execute()

    async def append_child_input_and_summary(self, write: ChildContextStartWrite) -> None:
        """写入 child 首条输入消息，并覆盖 child 可恢复摘要。"""
        pipeline = self._redis.pipeline()
        self._session_store.queue_append_child_message(
            pipeline,
            session_id=write.session_id,
            child_id=write.child_id,
            message=write.user_message,
            source_run_id=write.child_run_id,
            subagent_type=write.subagent_type,
        )
        self._session_store.queue_upsert_session_child_summary(
            pipeline,
            session_id=write.session_id,
            child_id=write.child_id,
            subagent_type=write.subagent_type,
            description=write.description,
        )
        await pipeline.execute()

    async def persist_child_run_terminal(self, write: ChildRunTerminalWrite) -> None:
        """持久化 child Run 终态，并按需追加 child 上下文终态消息。"""
        pipeline = self._redis.pipeline()
        self._run_store.queue_update_run_fields(
            pipeline=pipeline,
            run_id=write.child_run_id,
            status=write.status,
            finished_at=write.finished_at,
            output=write.output,
            error_code=write.error_code,
            error_message=write.error_message,
        )
        if write.terminal_message is not None:
            self._session_store.queue_append_child_message(
                pipeline,
                session_id=write.session_id,
                child_id=write.child_id,
                message=write.terminal_message,
                source_run_id=write.child_run_id,
                subagent_type=write.subagent_type,
            )
        await pipeline.execute()
