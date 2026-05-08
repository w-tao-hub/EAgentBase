"""Redis 大工具结果存储实现。

负责把超过预览阈值的工具完整输出落到 Redis，
并维护 session -> 工具结果 key 的关联索引，
供后续 QueryToolResult 工具查询与未来会话删除清理复用。
"""

from __future__ import annotations  # 启用未来注解，避免运行时前向引用问题。

from dataclasses import dataclass  # 导入数据类，表达读取到的持久化结果记录。
from datetime import datetime, timezone  # 导入时间工具，为持久化记录补时间戳。
import uuid  # 导入 UUID 生成器，生成稳定唯一的结果 key。
from typing import TYPE_CHECKING  # 导入类型检查标记，避免运行时循环依赖。

from app.infra.logging import get_logger  # 导入日志工厂，保持 infra 层日志风格一致。

logger = get_logger(__name__)  # 创建模块级日志器，便于记录持久化与清理行为。

if TYPE_CHECKING:  # 仅在类型检查阶段导入 Redis 类型，避免运行时增加依赖。
    from redis.asyncio import Redis


@dataclass(slots=True)
class PersistedToolResult:
    """单条已持久化工具结果记录。"""

    key: str  # Redis 中保存该结果的完整 key，供 QueryToolResult 直接引用。
    session_id: str  # 归属会话 ID，用于权限校验与未来级联删除。
    tool_name: str  # 原始工具名称，便于审计与排障。
    content: str  # 完整工具输出正文。
    created_at: datetime  # 写入时间，便于后续清理策略扩展。
    content_length: int  # 原始正文长度，避免调用方重复计算。


class RedisToolResultStore:
    """基于 Redis 的大工具结果存储。"""

    def __init__(self, redis: "Redis", key_prefix: str = "agent") -> None:
        """初始化工具结果存储。

        Args:
            redis: Redis 异步客户端实例。
            key_prefix: Redis key 前缀，用于命名空间隔离。
        """
        self._redis = redis  # 保存 Redis 客户端引用，供后续读写使用。
        self._key_prefix = key_prefix  # 保存 key 前缀，保证和其他 store 共用统一命名空间。

    def _tool_result_prefix(self) -> str:
        """返回工具结果 key 的公共前缀。"""
        return f"{self._key_prefix}:tool_result:"  # 统一使用固定命名空间，便于格式校验。

    def build_tool_result_key(self, result_id: str) -> str:
        """基于结果 ID 构造完整工具结果 key。"""
        return f"{self._tool_result_prefix()}{result_id}"  # 拼接完整 key。

    def is_tool_result_key(self, key: str) -> bool:
        """判断给定 key 是否属于当前 store 的工具结果命名空间。"""
        return key.startswith(self._tool_result_prefix())  # 仅按稳定前缀判断格式合法性。

    def _session_index_key(self, session_id: str) -> str:
        """返回 session 级工具结果索引 key。"""
        return f"{self._key_prefix}:session_tool_results:{session_id}"  # 用集合维护该 session 下全部大结果 key。

    async def persist_result(self, session_id: str, tool_name: str, content: str) -> str:
        """持久化一条完整工具结果，并建立 session 索引。"""
        result_id = str(uuid.uuid4())  # 为本次持久化生成稳定唯一的结果 ID。
        result_key = self.build_tool_result_key(result_id)  # 构造完整 Redis key。
        created_at = datetime.now(timezone.utc).isoformat()  # 使用 UTC ISO 时间保存写入时刻。
        payload = {  # 构造 Redis hash 字段，避免后续再手工拼接。
            "session_id": session_id,
            "tool_name": tool_name,
            "content": content,
            "created_at": created_at,
            "content_length": str(len(content)),
        }

        pipeline = self._redis.pipeline()  # 将正文落库与 session 索引建立合并到一次 Redis 往返。
        pipeline.hset(result_key, mapping=payload)  # 先排入正文写入命令，保持原有 hash 结构不变。
        pipeline.sadd(self._session_index_key(session_id), result_key)  # 再排入 session 级索引写入命令，避免额外一次 await。
        await pipeline.execute()  # 统一执行两条无数据依赖的写命令，减少网络往返次数。
        logger.info(
            "已持久化大工具结果: session_id=%s, tool_name=%s, key=%s, content_length=%d",
            session_id,
            tool_name,
            result_key,
            len(content),
        )
        return result_key  # 返回可直接展示与查询的 key。

    async def get_result(self, key: str, session_id: str) -> PersistedToolResult | None:
        """按 key 读取当前 session 下的完整工具结果。"""
        if not self.is_tool_result_key(key):  # 先做命名空间格式校验，减少无意义 Redis 访问。
            return None  # 非法 key 直接返回空，交给上层转成稳定错误提示。

        in_session_index = await self._redis.sismember(self._session_index_key(session_id), key)  # 校验 key 是否属于当前 session。
        if not in_session_index:  # 不属于当前会话时直接拒绝读取。
            return None

        data = await self._redis.hgetall(key)  # 从 Redis 读取完整结果 hash。
        if not data:  # 处理索引存在但正文已被清理的异常场景。
            logger.warning("工具结果索引存在但正文缺失: session_id=%s, key=%s", session_id, key)
            return None

        return PersistedToolResult(  # 将 Redis 字段组装成强类型记录对象。
            key=key,
            session_id=data["session_id"],
            tool_name=data["tool_name"],
            content=data["content"],
            created_at=datetime.fromisoformat(data["created_at"]),
            content_length=int(data["content_length"]),
        )

    async def delete_session_results(self, session_id: str) -> int:
        """删除某个 session 下的所有大工具结果及其索引。"""
        index_key = self._session_index_key(session_id)  # 先定位 session 级索引集合。
        result_keys = sorted(await self._redis.smembers(index_key))  # 读取全部结果 key，排序便于测试稳定。
        deleted_count = 0  # 记录成功删除的正文条数。

        if result_keys:  # 只有存在正文 key 时才执行批量删除。
            deleted_count = await self._redis.delete(*result_keys)  # 一次性删除所有正文 key。
        await self._redis.delete(index_key)  # 无论是否有正文，都清理索引集合本身。

        logger.info(
            "已清理 session 下的大工具结果: session_id=%s, deleted_count=%d",
            session_id,
            deleted_count,
        )
        return int(deleted_count)  # 显式转 int，统一返回值类型。
