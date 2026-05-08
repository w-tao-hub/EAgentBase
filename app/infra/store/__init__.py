"""Redis 存储层模块。

该模块提供基于 Redis 的持久化存储实现，包括：
- RedisSessionStore: 会话元数据与消息历史存储
- RedisRunStore: Run 状态持久化
- RedisLockStore: 分布式锁实现
"""

from __future__ import annotations  # 启用未来注解

from app.infra.store.redis_session_store import RedisSessionStore  # 导出 SessionStore
from app.infra.store.redis_run_store import RedisRunStore  # 导出 RunStore
from app.infra.store.redis_lock_store import RedisLockStore  # 导出 LockStore
from app.infra.store.redis_task_store import RedisTaskStore  # 导出 TaskStore
from app.infra.store.redis_tool_result_store import RedisToolResultStore  # 导出大工具结果存储。

__all__ = [  # 定义模块公开接口
    "RedisSessionStore",
    "RedisRunStore",
    "RedisLockStore",
    "RedisTaskStore",
    "RedisToolResultStore",
]
