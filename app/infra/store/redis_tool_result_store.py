"""Redis 大工具结果存储实现。

负责把超过预览阈值的工具完整输出落到 Redis，
并维护 session -> 工具结果 key 的关联索引，
供后续 QueryToolResult 工具查询与未来会话删除清理复用。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import uuid
from typing import TYPE_CHECKING

from app.infra.logging import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    from redis.asyncio import Redis


@dataclass(slots=True)
class PersistedToolResult:
    """单条已持久化工具结果记录。"""

    key: str
    session_id: str
    tool_name: str
    content: str
    created_at: datetime
    content_length: int


class RedisToolResultStore:
    """基于 Redis 的大工具结果存储。"""

    def __init__(self, redis: "Redis", key_prefix: str = "agent") -> None:
        self._redis = redis
        self._key_prefix = key_prefix

    def _tool_result_prefix(self) -> str:
        return f"{self._key_prefix}:tool_result:"

    def build_tool_result_key(self, result_id: str) -> str:
        return f"{self._tool_result_prefix()}{result_id}"

    def is_tool_result_key(self, key: str) -> bool:
        return key.startswith(self._tool_result_prefix())

    def _session_index_key(self, session_id: str) -> str:
        return f"{self._key_prefix}:session_tool_results:{session_id}"

    async def persist_result(self, session_id: str, tool_name: str, content: str) -> str:
        """持久化工具结果并建立 session 索引。"""
        result_id = str(uuid.uuid4())
        result_key = self.build_tool_result_key(result_id)
        created_at = datetime.now(timezone.utc).isoformat()
        payload = {
            "session_id": session_id,
            "tool_name": tool_name,
            "content": content,
            "created_at": created_at,
            "content_length": str(len(content)),
        }

        pipeline = self._redis.pipeline()
        pipeline.hset(result_key, mapping=payload)
        pipeline.sadd(self._session_index_key(session_id), result_key)
        await pipeline.execute()
        logger.info(
            "已持久化大工具结果: session_id=%s, tool_name=%s, key=%s, content_length=%d",
            session_id,
            tool_name,
            result_key,
            len(content),
        )
        return result_key

    async def get_result(self, key: str, session_id: str) -> PersistedToolResult | None:
        """按 key 读取当前 session 下的完整工具结果。"""
        if not self.is_tool_result_key(key):
            return None

        in_session_index = await self._redis.sismember(self._session_index_key(session_id), key)
        if not in_session_index:
            return None

        data = await self._redis.hgetall(key)
        if not data:
            logger.warning("工具结果索引存在但正文缺失: session_id=%s, key=%s", session_id, key)
            return None

        return PersistedToolResult(
            key=key,
            session_id=data["session_id"],
            tool_name=data["tool_name"],
            content=data["content"],
            created_at=datetime.fromisoformat(data["created_at"]),
            content_length=int(data["content_length"]),
        )

    async def delete_session_results(self, session_id: str) -> int:
        """删除 session 下的所有大工具结果及其索引。"""
        index_key = self._session_index_key(session_id)
        result_keys = sorted(await self._redis.smembers(index_key))
        deleted_count = 0

        if result_keys:
            deleted_count = await self._redis.delete(*result_keys)
        await self._redis.delete(index_key)

        logger.info(
            "已清理 session 下的大工具结果: session_id=%s, deleted_count=%d",
            session_id,
            deleted_count,
        )
        return int(deleted_count)
