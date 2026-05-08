"""RedisLockStore 实现。

提供基于 Redis 的分布式锁实现，使用 SET NX EX 获取锁，
使用 Lua 脚本进行 owner 校验释放与续期。
"""

from __future__ import annotations  # 启用未来注解

from typing import TYPE_CHECKING  # 导入类型检查标记

from app.infra.logging import get_logger  # 导入日志获取函数

# 获取模块级日志器
logger = get_logger(__name__)

if TYPE_CHECKING:  # 仅在类型检查时导入
    from redis.asyncio import Redis  # 异步 Redis 客户端类型


# Lua 脚本：释放锁时进行 owner 校验
# 只有当前持有锁的 run_id 才能释放锁
RELEASE_LOCK_SCRIPT = """
local lock_key = KEYS[1]  -- 锁的 key
local expected_run_id = ARGV[1]  -- 期望的 run_id（持有者）

local current_run_id = redis.call("GET", lock_key)  -- 获取当前锁的值
if current_run_id == expected_run_id then  -- 如果当前持有者匹配
    return redis.call("DEL", lock_key)  -- 删除锁，返回 1 表示成功
else
    return 0  -- 不匹配，返回 0 表示失败
end
"""

# Lua 脚本：续期锁时进行 owner 校验
# 只有当前持有锁的 run_id 才能刷新 TTL
EXTEND_LOCK_SCRIPT = """
local lock_key = KEYS[1]  -- 锁的 key
local expected_run_id = ARGV[1]  -- 期望的 run_id（持有者）
local ttl_seconds = tonumber(ARGV[2])  -- 新的过期时间（秒）

local current_run_id = redis.call("GET", lock_key)  -- 获取当前锁的值
if current_run_id == expected_run_id then  -- 只有 owner 匹配时才允许续期
    return redis.call("EXPIRE", lock_key, ttl_seconds)  -- 刷新 TTL，返回 1 表示成功
else
    return 0  -- 锁不存在或 owner 不匹配，续期失败
end
"""


class RedisLockStore:
    """基于 Redis 的分布式锁存储实现。

    使用 SET key value NX EX seconds 原子操作获取锁，
    使用 Lua 脚本进行 owner 校验释放，确保只有持有者能释放锁。

    Key 结构：{prefix}:lock:{session_id}
    Value：持有锁的 run_id
    """

    def __init__(self, redis: Redis, key_prefix: str = "agent") -> None:  # 构造函数
        """初始化 LockStore。

        Args:
            redis: Redis 异步客户端实例
            key_prefix: Redis key 前缀，用于命名空间隔离
        """
        self._redis = redis  # 保存 Redis 客户端引用
        self._key_prefix = key_prefix  # 保存 key 前缀
        # 尝试预编译 Lua 脚本，fakeredis 不支持则回退到手动实现
        self._release_script = self._try_register_script(redis, RELEASE_LOCK_SCRIPT)  # 尝试注册释放脚本
        self._extend_script = self._try_register_script(redis, EXTEND_LOCK_SCRIPT)  # 尝试注册续期脚本

    def _try_register_script(self, redis: Redis, script: str):  # 尝试注册 Lua 脚本
        """尝试注册 Lua 脚本，如果不支持则返回 None。"""
        try:
            return redis.register_script(script)  # 注册 Lua 脚本
        except Exception:  # 某些 Redis 客户端可能不支持
            return None  # 返回 None 表示使用回退实现

    def _lock_key(self, session_id: str) -> str:  # 生成锁 key
        """生成锁的 Redis key。"""
        return f"{self._key_prefix}:lock:{session_id}"  # 拼接 key

    async def acquire(self, session_id: str, run_id: str, ttl_seconds: int) -> bool:  # 获取锁
        """尝试获取会话锁。

        使用 SET key value NX EX seconds 原子操作，
        只在 key 不存在时设置成功。

        Args:
            session_id: 会话唯一标识
            run_id: 运行唯一标识（作为锁的 value）
            ttl_seconds: 锁的过期时间（秒）

        Returns:
            True 表示获取成功，False 表示锁已被占用
        """
        lock_key = self._lock_key(session_id)  # 获取锁 key
        # 使用 SET NX EX 原子操作尝试获取锁
        # nx=True: 只在 key 不存在时设置
        # ex=ttl_seconds: 设置过期时间
        result = await self._redis.set(  # 执行 SET 命令
            lock_key,  # key
            run_id,  # value（持有者 run_id）
            nx=True,  # 只在 key 不存在时设置
            ex=ttl_seconds,  # 过期时间（秒）
        )
        return result is not None  # SET 成功返回 True，失败返回 None

    async def get_active_run_id(self, session_id: str) -> str | None:  # 获取活跃 run_id
        """获取当前持有锁的 run_id。

        Args:
            session_id: 会话唯一标识

        Returns:
            持有锁的 run_id，如果锁不存在则返回 None
        """
        lock_key = self._lock_key(session_id)  # 获取锁 key
        # 使用 GET 获取锁的值
        run_id = await self._redis.get(lock_key)  # 从 Redis 读取
        return run_id  # 返回 run_id（可能为 None）

    async def extend(self, session_id: str, run_id: str, ttl_seconds: int) -> bool:  # 续期锁
        """续期会话锁（带 owner 校验）。

        优先使用 Lua 脚本原子地检查 owner 并刷新 TTL，
        如果 Lua 脚本不可用则使用 GET + EXPIRE 组合回退。

        Args:
            session_id: 会话唯一标识
            run_id: 运行唯一标识（必须是当前持有者）
            ttl_seconds: 新的过期时间（秒）

        Returns:
            True 表示续期成功，False 表示锁不存在或持有者不匹配
        """
        lock_key = self._lock_key(session_id)  # 获取锁 key

        # 如果 Lua 脚本可用，优先使用原子续期实现，避免 owner 检查与 EXPIRE 之间出现竞态
        if self._extend_script is not None:  # Lua 脚本可用
            try:  # 尝试执行 Lua 脚本
                result = await self._extend_script(  # 执行 Lua 脚本
                    keys=[lock_key],  # KEYS 数组
                    args=[run_id, ttl_seconds],  # 传入 owner 与新 TTL
                )
                return result == 1  # 返回 1 表示续期成功
            except Exception:  # Lua 脚本执行失败时回退到普通实现
                pass  # 继续执行下面的回退逻辑

        # 回退实现：先 GET 检查 owner，再 EXPIRE 刷新 TTL
        # 注意：该路径不是原子的，但能兼容 fakeredis 等不支持 Lua 的测试环境
        current_run_id = await self._redis.get(lock_key)  # 获取当前持有者
        if current_run_id is None:  # 锁不存在
            return False  # 续期失败
        if current_run_id != run_id:  # 持有者不匹配
            return False  # 续期失败
        result = await self._redis.expire(lock_key, ttl_seconds)  # 刷新锁 TTL
        return result is True or result == 1  # redis-py 可能返回 bool 或 int

    async def release(self, session_id: str, run_id: str) -> bool:  # 释放锁
        """释放会话锁（带 owner 校验）。

        优先使用 Lua 脚本原子地检查并删除锁，
        如果 Lua 脚本不可用则使用 GET + DEL 组合（非原子但功能正确）。
        只有当前持有锁的 run_id 才能释放成功。

        Args:
            session_id: 会话唯一标识
            run_id: 运行唯一标识（必须是当前持有者）

        Returns:
            True 表示释放成功，False 表示锁不存在或持有者不匹配
        """
        lock_key = self._lock_key(session_id)  # 获取锁 key

        # 如果 Lua 脚本可用，使用 Lua 脚本实现原子释放
        if self._release_script is not None:  # Lua 脚本可用
            try:  # 尝试执行 Lua 脚本
                result = await self._release_script(  # 执行 Lua 脚本
                    keys=[lock_key],  # KEYS 数组
                    args=[run_id],  # ARGV 数组
                )
                return result == 1  # Lua 脚本返回 1 表示删除成功，0 表示失败
            except Exception:  # Lua 脚本执行失败（如 fakeredis 不支持 evalsha）
                pass  # 回退到手动实现

        # 回退实现：先 GET 检查 owner，再 DEL 删除
        # 注意：这不是原子的，但在测试场景下足够正确
        current_run_id = await self._redis.get(lock_key)  # 获取当前持有者
        if current_run_id is None:  # 锁不存在
            return False  # 释放失败
        if current_run_id != run_id:  # 持有者不匹配
            return False  # 释放失败
        # 持有者匹配，删除锁
        result = await self._redis.delete(lock_key)  # 删除锁
        return result == 1  # 删除成功返回 True
