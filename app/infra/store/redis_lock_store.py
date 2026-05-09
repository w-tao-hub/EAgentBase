"""RedisLockStore 实现。

提供基于 Redis 的分布式锁实现，使用 SET NX EX 获取锁，
使用 Lua 脚本进行 owner 校验释放与续期。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.infra.logging import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    from redis.asyncio import Redis


# Lua 脚本：释放锁时校验 owner，只有持有者才能释放
RELEASE_LOCK_SCRIPT = """
local lock_key = KEYS[1]
local expected_run_id = ARGV[1]

local current_run_id = redis.call("GET", lock_key)
if current_run_id == expected_run_id then
    return redis.call("DEL", lock_key)
else
    return 0
end
"""

# Lua 脚本：续期锁时校验 owner，只有持有者才能刷新 TTL
EXTEND_LOCK_SCRIPT = """
local lock_key = KEYS[1]
local expected_run_id = ARGV[1]
local ttl_seconds = tonumber(ARGV[2])

local current_run_id = redis.call("GET", lock_key)
if current_run_id == expected_run_id then
    return redis.call("EXPIRE", lock_key, ttl_seconds)
else
    return 0
end
"""


class RedisLockStore:
    """基于 Redis 的分布式锁（SET NX EX + Lua owner 校验）。"""

    def __init__(self, redis: Redis, key_prefix: str = "agent") -> None:
        self._redis = redis
        self._key_prefix = key_prefix
        self._release_script = self._try_register_script(redis, RELEASE_LOCK_SCRIPT)
        self._extend_script = self._try_register_script(redis, EXTEND_LOCK_SCRIPT)

    def _try_register_script(self, redis: Redis, script: str):
        """注册 Lua 脚本，不支持时返回 None。"""
        try:
            return redis.register_script(script)
        except Exception:
            return None

    def _lock_key(self, session_id: str) -> str:
        return f"{self._key_prefix}:lock:{session_id}"

    async def acquire(self, session_id: str, run_id: str, ttl_seconds: int) -> bool:
        """尝试获取会话锁（SET NX EX）。"""
        lock_key = self._lock_key(session_id)
        result = await self._redis.set(
            lock_key,
            run_id,
            nx=True,
            ex=ttl_seconds,
        )
        return result is not None

    async def get_active_run_id(self, session_id: str) -> str | None:
        lock_key = self._lock_key(session_id)
        run_id = await self._redis.get(lock_key)
        return run_id

    async def extend(self, session_id: str, run_id: str, ttl_seconds: int) -> bool:
        """续期锁（优先 Lua 原子操作，回退 GET+EXPIRE）。"""
        lock_key = self._lock_key(session_id)

        if self._extend_script is not None:
            try:
                result = await self._extend_script(
                    keys=[lock_key],
                    args=[run_id, ttl_seconds],
                )
                return result == 1
            except Exception:
                pass

        # 回退：非原子 GET+EXPIRE，兼容 fakeredis
        current_run_id = await self._redis.get(lock_key)
        if current_run_id is None:
            return False
        if current_run_id != run_id:
            return False
        result = await self._redis.expire(lock_key, ttl_seconds)
        return result is True or result == 1

    async def release(self, session_id: str, run_id: str) -> bool:
        """释放锁（优先 Lua 原子操作，回退 GET+DEL）。"""
        lock_key = self._lock_key(session_id)

        if self._release_script is not None:
            try:
                result = await self._release_script(
                    keys=[lock_key],
                    args=[run_id],
                )
                return result == 1
            except Exception:
                pass

        # 回退：非原子 GET+DEL，兼容 fakeredis
        current_run_id = await self._redis.get(lock_key)
        if current_run_id is None:
            return False
        if current_run_id != run_id:
            return False
        result = await self._redis.delete(lock_key)
        return result == 1
