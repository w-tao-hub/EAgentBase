"""RedisSessionStore 实现。

提供会话元数据、主会话上下文与 child 上下文的 Redis 持久化存储。
本次仅面向新结构实现，不再兼容未上线时期的旧 key / 旧消息格式。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import TYPE_CHECKING, Any

from app.core.models.session import Session
from app.core.models.stored_message import StoredMessage
from app.infra.logging import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    from redis.asyncio import Redis


@dataclass(slots=True)
class ContextSummaryState:
    """会话最近一次上下文摘要的边界状态。"""

    summary_message_id: str
    active_start_message_id: str | None
    summary_offset: int | None = None
    active_start_offset: int | None = None


@dataclass(frozen=True, slots=True)
class SessionChildSummary:
    """表示当前 session 下一个可恢复子代理的最新摘要。"""

    resume_id: str
    subagent_type: str
    description: str


@dataclass(slots=True, frozen=True)
class ContextKeySet:
    """同一条上下文消息流所需的 Redis key 组合。"""

    messages_key: str
    dirty_key: str
    summary_state_key: str


class RedisSessionStore:
    """基于 Redis 的会话存储实现。"""

    def __init__(self, redis: Redis, key_prefix: str = "agent") -> None:
        self._redis = redis
        self._key_prefix = key_prefix

    def _session_key(self, session_id: str) -> str:
        return f"{self._key_prefix}:session:{session_id}"

    def _session_main_messages_key(self, session_id: str) -> str:
        return f"{self._key_prefix}:session_main_messages:{session_id}"

    def _session_main_messages_dirty_key(self, session_id: str) -> str:
        return f"{self._key_prefix}:session_main_messages_dirty:{session_id}"

    def _session_main_context_summary_state_key(self, session_id: str) -> str:
        return f"{self._key_prefix}:session_main_context_summary_state:{session_id}"

    def _session_runs_key(self, session_id: str) -> str:
        return f"{self._key_prefix}:session_runs:{session_id}"

    def _session_children_key(self, session_id: str) -> str:
        return f"{self._key_prefix}:session_children:{session_id}"

    def _child_context_messages_key(self, session_id: str, child_id: str) -> str:
        return f"{self._key_prefix}:child_context_messages:{session_id}:{child_id}"

    def _child_context_messages_dirty_key(self, session_id: str, child_id: str) -> str:
        return f"{self._key_prefix}:child_context_messages_dirty:{session_id}:{child_id}"

    def _child_context_summary_state_key(self, session_id: str, child_id: str) -> str:
        return f"{self._key_prefix}:child_context_summary_state:{session_id}:{child_id}"

    def _main_context_keys(self, session_id: str) -> ContextKeySet:
        return ContextKeySet(
            messages_key=self._session_main_messages_key(session_id),
            dirty_key=self._session_main_messages_dirty_key(session_id),
            summary_state_key=self._session_main_context_summary_state_key(session_id),
        )

    def _child_context_keys(self, session_id: str, child_id: str) -> ContextKeySet:
        return ContextKeySet(
            messages_key=self._child_context_messages_key(session_id, child_id),
            dirty_key=self._child_context_messages_dirty_key(session_id, child_id),
            summary_state_key=self._child_context_summary_state_key(session_id, child_id),
        )

    async def create_session(self, session: Session) -> Session:
        session_key = self._session_key(session.session_id)
        session_data = {
            "session_id": session.session_id,
            "agent_id": session.agent_id,
            "created_at": session.created_at.isoformat(),
        }
        await self._redis.hset(session_key, mapping=session_data)
        return session

    async def get_session(self, session_id: str) -> Session | None:
        session_key = self._session_key(session_id)
        data = await self._redis.hgetall(session_key)
        if not data:
            logger.debug("会话不存在: session_id=%s", session_id)
            return None
        logger.debug("会话查询成功: session_id=%s", session_id)
        return Session(
            session_id=data["session_id"],
            agent_id=data["agent_id"],
            created_at=datetime.fromisoformat(data["created_at"]),
        )

    async def add_session_run(
        self,
        session_id: str,
        run_id: str,
        created_at: datetime | None = None,
        created_at_ts: float | None = None,
    ) -> None:
        if created_at_ts is not None:
            score = created_at_ts
        else:
            score = (created_at or datetime.now(timezone.utc)).timestamp()
        await self._redis.zadd(self._session_runs_key(session_id), {run_id: score})

    def queue_add_session_run(
        self,
        pipeline: Any,
        session_id: str,
        run_id: str,
        created_at: datetime | None = None,
        created_at_ts: float | None = None,
    ) -> None:
        if created_at_ts is not None:
            score = created_at_ts
        else:
            score = (created_at or datetime.now(timezone.utc)).timestamp()
        pipeline.zadd(self._session_runs_key(session_id), {run_id: score})

    async def list_session_runs(self, session_id: str) -> list[str]:
        return await self._redis.zrange(self._session_runs_key(session_id), 0, -1)

    async def list_session_run_ids(self, session_id: str) -> list[str]:
        return await self.list_session_runs(session_id)

    async def ensure_session_child_registered(self, session_id: str, child_id: str) -> None:
        """使用 HSETNX 确保 child 记录存在但不破坏已有摘要。"""
        key = self._session_children_key(session_id)
        placeholder = SessionChildSummary(
            resume_id=child_id,
            subagent_type="",
            description="",
        )
        await self._redis.hsetnx(key, child_id, self._serialize_session_child_summary(placeholder))

    def queue_upsert_session_child_summary(
        self,
        pipeline: Any,
        session_id: str,
        child_id: str,
        subagent_type: str,
        description: str,
    ) -> None:
        summary = SessionChildSummary(
            resume_id=child_id,
            subagent_type=subagent_type,
            description=description,
        )
        pipeline.hset(
            self._session_children_key(session_id),
            child_id,
            self._serialize_session_child_summary(summary),
        )

    async def list_session_children(self, session_id: str) -> list[str]:
        child_ids = await self._redis.hkeys(self._session_children_key(session_id))
        return sorted(child_ids)

    async def list_session_child_ids(self, session_id: str) -> list[str]:
        return await self.list_session_children(session_id)

    async def upsert_session_child_summary(
        self,
        session_id: str,
        child_id: str,
        subagent_type: str,
        description: str,
    ) -> None:
        summary = SessionChildSummary(
            resume_id=child_id,
            subagent_type=subagent_type,
            description=description,
        )
        await self._redis.hset(
            self._session_children_key(session_id),
            child_id,
            self._serialize_session_child_summary(summary),
        )

    async def list_session_child_summaries(self, session_id: str) -> list[SessionChildSummary]:
        payload = await self._redis.hgetall(self._session_children_key(session_id))
        summaries: list[SessionChildSummary] = []
        for child_id in sorted(payload):
            summaries.append(self._deserialize_session_child_summary(payload[child_id], fallback_child_id=child_id))
        return summaries

    @staticmethod
    def _serialize_session_child_summary(summary: SessionChildSummary) -> str:
        return json.dumps(
            {
                "resume_id": summary.resume_id,
                "subagent_type": summary.subagent_type,
                "description": summary.description,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _deserialize_session_child_summary(payload: str, *, fallback_child_id: str) -> SessionChildSummary:
        data = json.loads(payload)
        return SessionChildSummary(
            resume_id=str(data.get("resume_id") or fallback_child_id),
            subagent_type=str(data.get("subagent_type") or ""),
            description=str(data.get("description") or ""),
        )

    async def get_main_message_count(self, session_id: str) -> int:
        return await self._get_message_count_from_keys(self._main_context_keys(session_id))

    async def append_main_message(
        self,
        session_id: str,
        message: StoredMessage,
        source_run_id: str | None = None,
        child_id: str | None = None,
    ) -> None:
        await self._append_message_to_keys(
            self._main_context_keys(session_id),
            message,
            source_run_id=source_run_id,
            child_id=child_id,
        )

    def queue_append_main_message(
        self,
        pipeline: Any,
        session_id: str,
        message: StoredMessage,
        source_run_id: str | None = None,
        child_id: str | None = None,
    ) -> None:
        self._queue_append_message_to_keys(
            pipeline,
            self._main_context_keys(session_id),
            message,
            source_run_id=source_run_id,
            child_id=child_id,
        )

    async def list_main_messages(self, session_id: str, start: int = 0, end: int = -1) -> list[StoredMessage]:
        return await self._list_messages_from_keys(
            self._main_context_keys(session_id),
            start=start,
            end=end,
        )

    async def get_main_context_summary_state(self, session_id: str) -> ContextSummaryState | None:
        return await self._get_context_summary_state_from_keys(self._main_context_keys(session_id))

    async def append_main_context_summary(
        self,
        session_id: str,
        message: StoredMessage,
        active_start_message: StoredMessage | None,
        active_start_offset: int | None = None,
    ) -> ContextSummaryState:
        return await self._append_context_summary_to_keys(
            self._main_context_keys(session_id),
            message,
            active_start_message=active_start_message,
            active_start_offset=active_start_offset,
            source_run_id=None,
            child_id=None,
        )

    async def list_main_active_messages(self, session_id: str) -> list[StoredMessage]:
        return await self._list_active_messages_from_keys(self._main_context_keys(session_id))

    async def list_active_main_messages(self, session_id: str) -> list[StoredMessage]:
        return await self.list_main_active_messages(session_id)

    async def list_main_active_messages_with_indices(self, session_id: str) -> tuple[list[StoredMessage], list[int]]:
        return await self._list_active_messages_with_indices_from_keys(self._main_context_keys(session_id))

    async def list_active_main_messages_with_indices(self, session_id: str) -> tuple[list[StoredMessage], list[int]]:
        return await self.list_main_active_messages_with_indices(session_id)

    async def mark_main_history_dirty(self, session_id: str) -> None:
        await self._mark_history_dirty_by_keys(self._main_context_keys(session_id))

    async def is_main_history_dirty(self, session_id: str) -> bool:
        return await self._is_history_dirty_by_keys(self._main_context_keys(session_id))

    async def get_child_message_count(self, session_id: str, child_id: str) -> int:
        return await self._get_message_count_from_keys(self._child_context_keys(session_id, child_id))

    async def append_child_message(
        self,
        session_id: str,
        child_id: str,
        message: StoredMessage,
        source_run_id: str | None = None,
        subagent_type: str | None = None,
    ) -> None:
        await self.ensure_session_child_registered(session_id, child_id)
        await self._append_message_to_keys(
            self._child_context_keys(session_id, child_id),
            message,
            source_run_id=source_run_id,
            child_id=child_id,
            subagent_type=subagent_type,
        )

    def queue_append_child_message(
        self,
        pipeline: Any,
        session_id: str,
        child_id: str,
        message: StoredMessage,
        source_run_id: str | None = None,
        subagent_type: str | None = None,
    ) -> None:
        self._queue_append_message_to_keys(
            pipeline,
            self._child_context_keys(session_id, child_id),
            message,
            source_run_id=source_run_id,
            child_id=child_id,
            subagent_type=subagent_type,
        )

    async def list_child_messages(
        self,
        session_id: str,
        child_id: str,
        start: int = 0,
        end: int = -1,
    ) -> list[StoredMessage]:
        return await self._list_messages_from_keys(self._child_context_keys(session_id, child_id), start=start, end=end)

    async def list_child_context_messages(
        self,
        session_id: str,
        child_id: str,
        start: int = 0,
        end: int = -1,
    ) -> list[StoredMessage]:
        return await self.list_child_messages(session_id, child_id, start=start, end=end)

    async def get_child_context_summary_state(self, session_id: str, child_id: str) -> ContextSummaryState | None:
        return await self._get_context_summary_state_from_keys(self._child_context_keys(session_id, child_id))

    async def append_child_context_summary(
        self,
        session_id: str,
        child_id: str,
        message: StoredMessage,
        active_start_message: StoredMessage | None,
        active_start_offset: int | None = None,
    ) -> ContextSummaryState:
        await self.ensure_session_child_registered(session_id, child_id)
        return await self._append_context_summary_to_keys(
            self._child_context_keys(session_id, child_id),
            message,
            active_start_message=active_start_message,
            active_start_offset=active_start_offset,
            source_run_id=None,
            child_id=child_id,
        )

    async def list_child_active_messages(self, session_id: str, child_id: str) -> list[StoredMessage]:
        return await self._list_active_messages_from_keys(self._child_context_keys(session_id, child_id))

    async def list_child_active_messages_with_indices(
        self,
        session_id: str,
        child_id: str,
    ) -> tuple[list[StoredMessage], list[int]]:
        return await self._list_active_messages_with_indices_from_keys(self._child_context_keys(session_id, child_id))

    async def mark_child_history_dirty(self, session_id: str, child_id: str) -> None:
        await self.ensure_session_child_registered(session_id, child_id)
        await self._mark_history_dirty_by_keys(self._child_context_keys(session_id, child_id))

    async def is_child_history_dirty(self, session_id: str, child_id: str) -> bool:
        return await self._is_history_dirty_by_keys(self._child_context_keys(session_id, child_id))

    async def get_message_count(self, session_id: str) -> int:
        return await self.get_main_message_count(session_id)

    async def append_message(
        self,
        session_id: str,
        message: StoredMessage,
        source_run_id: str | None = None,
    ) -> None:
        await self.append_main_message(session_id, message, source_run_id=source_run_id)

    def queue_append_message(
        self,
        pipeline: Any,
        session_id: str,
        message: StoredMessage,
        source_run_id: str | None = None,
    ) -> None:
        self.queue_append_main_message(
            pipeline,
            session_id,
            message,
            source_run_id=source_run_id,
        )

    async def list_messages(self, session_id: str, start: int = 0, end: int = -1) -> list[StoredMessage]:
        return await self.list_main_messages(session_id, start=start, end=end)

    async def get_context_summary_state(self, session_id: str) -> ContextSummaryState | None:
        return await self.get_main_context_summary_state(session_id)

    async def append_context_summary(
        self,
        session_id: str,
        message: StoredMessage,
        active_start_message: StoredMessage | None,
        active_start_offset: int | None = None,
    ) -> ContextSummaryState:
        return await self.append_main_context_summary(
            session_id,
            message,
            active_start_message=active_start_message,
            active_start_offset=active_start_offset,
        )

    async def list_active_messages(self, session_id: str) -> list[StoredMessage]:
        return await self.list_main_active_messages(session_id)

    async def list_active_messages_with_indices(self, session_id: str) -> tuple[list[StoredMessage], list[int]]:
        return await self.list_main_active_messages_with_indices(session_id)

    async def mark_history_dirty(self, session_id: str) -> None:
        await self.mark_main_history_dirty(session_id)

    async def is_history_dirty(self, session_id: str) -> bool:
        return await self.is_main_history_dirty(session_id)

    @staticmethod
    def _serialize_message(
        message: StoredMessage,
        *,
        source_run_id: str | None = None,
        child_id: str | None = None,
        subagent_type: str | None = None,
    ) -> str:
        """序列化 StoredMessage 为 JSON（主会话不保留 subagent_type）。"""
        if child_id is None:
            message = message.model_copy(
                update={
                    "meta": message.meta.model_copy(
                        update={"child_id": None, "subagent_type": None}
                    )
                },
                deep=True,
            )
        elif subagent_type is None:
            message = message.model_copy(
                update={
                    "meta": message.meta.model_copy(
                        update={"subagent_type": None}
                    )
                },
                deep=True,
            )
        stored_message = message.with_meta_overrides(
            source_run_id=source_run_id,
            child_id=child_id,
            subagent_type=subagent_type,
        )
        return json.dumps(stored_message.to_storage_dict(), ensure_ascii=False)

    async def delete_session_main_context(self, session_id: str) -> int:
        return int(
            await self._redis.delete(
                self._session_main_messages_key(session_id),
                self._session_main_messages_dirty_key(session_id),
                self._session_main_context_summary_state_key(session_id),
            )
        )

    async def delete_child_context(self, session_id: str, child_id: str) -> int:
        return int(
            await self._redis.delete(
                self._child_context_messages_key(session_id, child_id),
                self._child_context_messages_dirty_key(session_id, child_id),
                self._child_context_summary_state_key(session_id, child_id),
            )
        )

    async def delete_session_metadata_and_indices(self, session_id: str) -> int:
        return int(
            await self._redis.delete(
                self._session_key(session_id),
                self._session_runs_key(session_id),
                self._session_children_key(session_id),
            )
        )

    @staticmethod
    def _deserialize_message(message_json: str) -> StoredMessage:
        return StoredMessage.from_storage_dict(json.loads(message_json))

    async def _get_message_count_from_keys(self, context_keys: ContextKeySet) -> int:
        return await self._redis.llen(context_keys.messages_key)

    async def _append_message_to_keys(
        self,
        context_keys: ContextKeySet,
        message: StoredMessage,
        *,
        source_run_id: str | None = None,
        child_id: str | None = None,
        subagent_type: str | None = None,
    ) -> None:
        await self._redis.rpush(
            context_keys.messages_key,
            self._serialize_message(message, source_run_id=source_run_id, child_id=child_id, subagent_type=subagent_type),
        )

    def _queue_append_message_to_keys(
        self,
        pipeline: Any,
        context_keys: ContextKeySet,
        message: StoredMessage,
        *,
        source_run_id: str | None = None,
        child_id: str | None = None,
        subagent_type: str | None = None,
    ) -> None:
        pipeline.rpush(
            context_keys.messages_key,
            self._serialize_message(message, source_run_id=source_run_id, child_id=child_id, subagent_type=subagent_type),
        )

    async def _list_messages_from_keys(
        self,
        context_keys: ContextKeySet,
        start: int = 0,
        end: int = -1,
    ) -> list[StoredMessage]:
        message_jsons = await self._redis.lrange(context_keys.messages_key, start, end)
        return [self._deserialize_message(message_json) for message_json in message_jsons]

    async def _find_message_offset_by_id(self, messages_key: str, message_id: str) -> int | None:
        """按 message_id 顺序扫描查找消息在 Redis List 中的偏移。"""
        message_jsons = await self._redis.lrange(messages_key, 0, -1)
        for index, message_json in enumerate(message_jsons):
            if self._deserialize_message(message_json).message_id == message_id:
                return index
        return None

    async def _get_context_summary_state_from_keys(self, context_keys: ContextKeySet) -> ContextSummaryState | None:
        raw_value = await self._redis.get(context_keys.summary_state_key)
        if not raw_value:
            return None
        return self._deserialize_context_summary_state(raw_value)

    async def _append_context_summary_to_keys(
        self,
        context_keys: ContextKeySet,
        message: StoredMessage,
        active_start_message: StoredMessage | None,
        active_start_offset: int | None = None,
        source_run_id: str | None = None,
        child_id: str | None = None,
    ) -> ContextSummaryState:
        summary_offset = await self._redis.llen(context_keys.messages_key)
        message_json = self._serialize_message(
            message,
            source_run_id=source_run_id,
            child_id=child_id,
        )
        resolved_active_start_offset = active_start_offset
        if resolved_active_start_offset is None and active_start_message is not None:
            resolved_active_start_offset = await self._find_message_offset_by_id(
                context_keys.messages_key,
                active_start_message.message_id,
            )
        if resolved_active_start_offset is None:
            resolved_active_start_offset = summary_offset

        summary_state = ContextSummaryState(
            summary_message_id=message.message_id,
            active_start_message_id=(
                active_start_message.message_id if active_start_message is not None else None
            ),
            summary_offset=summary_offset,
            active_start_offset=resolved_active_start_offset,
        )
        summary_state_json = self._serialize_context_summary_state(summary_state)

        pipeline = self._redis.pipeline()
        pipeline.rpush(context_keys.messages_key, message_json)
        pipeline.set(context_keys.summary_state_key, summary_state_json)
        await pipeline.execute()
        return summary_state

    async def _list_active_messages_from_keys(self, context_keys: ContextKeySet) -> list[StoredMessage]:
        """读取指定上下文当前会参与构建的活动窗口。"""
        messages, summary_state = await asyncio.gather(
            self._list_messages_from_keys(context_keys),  # 并行读取完整历史，便于普通调用方直接复用。
            self._get_context_summary_state_from_keys(context_keys),  # 同时读取最近一次摘要边界状态。
        )
        active_messages, _active_indices = self.build_active_messages_with_indices(
            messages,
            summary_state,
        )  # 直接基于已拿到的历史与边界重建活动窗口，避免重复 Redis 读取。
        return active_messages  # 只返回活动消息列表，供普通调用方直接使用。

    async def _list_active_messages_with_indices_from_keys(
        self,
        context_keys: ContextKeySet,
    ) -> tuple[list[StoredMessage], list[int]]:
        """读取指定上下文当前活动窗口，并返回其在 Redis List 中的绝对偏移映射。"""
        summary_state = await self._get_context_summary_state_from_keys(context_keys)  # 先读取最近一次摘要边界。
        if summary_state is None:  # 未启用摘要压缩时，活动窗口就是完整历史。
            messages = await self._list_messages_from_keys(context_keys)  # 直接全量读取当前历史。
            return messages, list(range(len(messages)))  # 此时绝对偏移就是当前列表索引。

        start_offset = summary_state.active_start_offset  # 优先使用状态中缓存的活动窗口起点偏移。
        if start_offset is None:  # 旧状态未记录活动窗口偏移时，退化为从摘要位置开始读取。
            start_offset = summary_state.summary_offset
        if (
            start_offset is not None
            and summary_state.summary_offset is not None
            and start_offset <= summary_state.summary_offset
        ):  # 只有起点仍在摘要之前或等于摘要时，才可走范围读取快路径。
            ranged_messages = await self._list_messages_from_keys(context_keys, start=start_offset, end=-1)  # 只读取活动窗口连续尾段，避免再次全量 LRANGE。
            rebuilt_window = self._rebuild_active_window_from_ranged_messages(
                ranged_messages=ranged_messages,
                summary_state=summary_state,
                start_offset=start_offset,
            )
            if rebuilt_window is not None:  # 快路径命中且校验通过时，直接返回重建后的活动窗口。
                return rebuilt_window

        messages = await self._list_messages_from_keys(context_keys)  # 快路径失效时回退到完整历史，保证语义正确优先。
        return self.build_active_messages_with_indices(messages, summary_state)  # 继续沿用按 UUID 扫描的稳妥路径。

    @staticmethod
    def _serialize_context_summary_state(summary_state: ContextSummaryState) -> str:
        """把摘要边界状态序列化为 Redis JSON 文本。"""
        return json.dumps(
            {
                "summary_message_id": summary_state.summary_message_id,
                "active_start_message_id": summary_state.active_start_message_id,
                "summary_offset": summary_state.summary_offset,
                "active_start_offset": summary_state.active_start_offset,
            }
        )

    @staticmethod
    def _deserialize_context_summary_state(raw_value: str) -> ContextSummaryState:
        """把 Redis 中的摘要状态 JSON 文本反序列化为 ContextSummaryState。"""
        data = json.loads(raw_value)  # 解析 JSON，恢复消息唯一标识与偏移数据。
        return ContextSummaryState(
            summary_message_id=data["summary_message_id"],  # 恢复摘要消息 UUID。
            active_start_message_id=data.get("active_start_message_id") or None,  # 恢复活动窗口起点 UUID。
            summary_offset=data.get("summary_offset"),  # 恢复摘要消息偏移；旧状态缺失时保持 None。
            active_start_offset=data.get("active_start_offset"),  # 恢复活动窗口起点偏移；旧状态缺失时保持 None。
        )

    async def _mark_history_dirty_by_keys(self, context_keys: ContextKeySet) -> None:
        """标记指定上下文历史存在待修复问题。"""
        await self._redis.set(context_keys.dirty_key, "1")  # 写入固定标记值，表示该上下文历史需要后续排查。

    async def _is_history_dirty_by_keys(self, context_keys: ContextKeySet) -> bool:
        """查询指定上下文历史是否已被标记为 dirty。"""
        value = await self._redis.get(context_keys.dirty_key)  # 读取 dirty 标记值。
        return value == "1"  # 只有固定值 1 时才视为已打脏标记。

    @staticmethod
    def build_active_messages(
        messages: list[StoredMessage],
        summary_state: ContextSummaryState | None,
    ) -> list[StoredMessage]:
        """根据最近一次摘要边界重建活动消息窗口。"""
        active_messages, _active_indices = RedisSessionStore.build_active_messages_with_indices(messages, summary_state)  # 复用统一的重建逻辑，避免两套实现漂移。
        return active_messages  # 对外仅暴露消息列表，保持既有调用方不变。

    @staticmethod
    def build_active_messages_with_indices(
        messages: list[StoredMessage],
        summary_state: ContextSummaryState | None,
    ) -> tuple[list[StoredMessage], list[int]]:
        """根据最近一次摘要边界重建活动消息窗口，并保留索引映射。"""
        if summary_state is None:  # 从未压缩过时，完整历史就是活动窗口。
            return list(messages), list(range(len(messages)))

        summary_index: int | None = None
        active_start_index: int | None = None
        for index, message in enumerate(messages):
            if message.message_id == summary_state.summary_message_id:
                summary_index = index
            if summary_state.active_start_message_id is not None and message.message_id == summary_state.active_start_message_id:
                active_start_index = index

        if summary_index is None:  # 摘要消息未找到时，直接回退到完整历史。
            return list(messages), list(range(len(messages)))

        if active_start_index is None:  # 起点未找到，则活动窗口仅包含摘要消息及其之后的所有消息。
            active_messages: list[StoredMessage] = [messages[summary_index]]
            active_messages.extend(messages[summary_index + 1 :])
            active_indices: list[int] = [summary_index]
            active_indices.extend(range(summary_index + 1, len(messages)))
            return active_messages, active_indices

        if active_start_index < 0 or active_start_index > summary_index:  # 起点位置非法时回退完整历史。
            return list(messages), list(range(len(messages)))

        active_messages = [messages[summary_index]]  # 首位固定放最近一次摘要消息。
        active_messages.extend(messages[active_start_index:summary_index])  # 接上摘要前仍需保留的最近历史。
        active_messages.extend(messages[summary_index + 1 :])  # 最后接上摘要之后产生的所有新消息。
        active_indices = [summary_index]  # 首位索引固定对应摘要消息在完整历史中的位置。
        active_indices.extend(range(active_start_index, summary_index))  # 紧随其后的是摘要前保留窗口。
        active_indices.extend(range(summary_index + 1, len(messages)))  # 最后追加摘要之后产生的所有新消息。
        return active_messages, active_indices

    @staticmethod
    def _rebuild_active_window_from_ranged_messages(
        ranged_messages: list[StoredMessage],
        summary_state: ContextSummaryState,
        start_offset: int,
    ) -> tuple[list[StoredMessage], list[int]] | None:
        """根据连续尾段范围读取结果，重建"摘要优先"的活动窗口。"""
        if summary_state.summary_offset is None:  # 缺少摘要偏移时无法使用范围读取快路径。
            return None
        if start_offset > summary_state.summary_offset:  # 起点跑到摘要之后时，当前状态已不可信。
            return None
        summary_relative_index = summary_state.summary_offset - start_offset  # 计算摘要消息在尾段窗口中的相对位置。
        if summary_relative_index < 0 or summary_relative_index >= len(ranged_messages):  # 摘要不在读取结果中时，说明偏移已失效。
            return None
        summary_message = ranged_messages[summary_relative_index]  # 取出理论上的摘要消息，用于做 ID 校验。
        if summary_message.message_id != summary_state.summary_message_id:  # 摘要位置的消息 UUID 不匹配时，必须回退到完整扫描。
            return None
        if (
            summary_state.active_start_message_id is not None
            and start_offset < summary_state.summary_offset
            and ranged_messages
            and ranged_messages[0].message_id != summary_state.active_start_message_id
        ):  # 活动窗口起点 UUID 不匹配时，说明前缀被裁剪或状态已漂移，不能信任快路径。
            return None

        active_messages: list[StoredMessage] = [summary_message]
        active_indices: list[int] = [summary_state.summary_offset]
        for relative_index, message in enumerate(ranged_messages):  # 再按原尾段顺序追加除摘要外的所有消息。
            absolute_index = start_offset + relative_index  # 计算当前消息在 Redis List 中的绝对位置。
            if absolute_index == summary_state.summary_offset:  # 摘要消息已经放在首位，不需要重复加入。
                continue
            active_messages.append(message)  # 把保留窗口消息和摘要后的新消息依次追加到输出列表。
            active_indices.append(absolute_index)  # 同步记录对应绝对索引，供压缩策略后续复用。
        return active_messages, active_indices
