"""RedisTaskStore 实现。"""  # 模块级说明，声明当前文件负责任务存储。

from __future__ import annotations  # 启用未来注解，避免类型前向引用问题。

import json  # 导入 JSON 模块，用于任务对象序列化和反序列化。
from typing import TYPE_CHECKING  # 导入类型检查标记，避免运行时额外依赖。

from app.core.models.task import TaskItem  # 导入任务实体模型，用于存储读写。
from app.infra.logging import get_logger  # 导入日志工厂，用于记录存储层日志。

# 获取模块级日志器。  # 说明下方日志器仅用于当前模块输出。
logger = get_logger(__name__)  # 创建当前模块专用日志器。

if TYPE_CHECKING:  # 仅在类型检查阶段导入 Redis 类型。
    from redis.asyncio import Redis  # 导入异步 Redis 客户端类型。


class RedisTaskStore:  # 定义基于 Redis 的任务存储实现。
    """基于 Redis 的任务存储。"""  # 说明当前类负责会话级任务持久化。

    def __init__(self, redis: Redis, key_prefix: str = "agent") -> None:  # 初始化任务存储实例。
        """初始化 TaskStore。"""  # 说明构造函数只保存底层依赖和命名空间前缀。
        self._redis = redis  # 保存 Redis 客户端引用，供后续所有读写操作复用。
        self._key_prefix = key_prefix  # 保存 key 前缀，用于隔离不同部署环境的数据。

    def _tasks_key(self, session_id: str) -> str:  # 生成指定会话的任务哈希 key。
        """生成会话任务集合的 Redis key。"""  # 说明该 key 负责保存任务明细。
        return f"{self._key_prefix}:tasks:{session_id}"  # 返回会话任务哈希 key。

    def _task_counter_key(self, session_id: str) -> str:  # 生成指定会话的任务计数器 key。
        """生成会话任务 ID 计数器的 Redis key。"""  # 说明该 key 负责分配递增任务 ID。
        return f"{self._key_prefix}:task_counter:{session_id}"  # 返回会话任务计数器 key。

    async def next_task_id(self, session_id: str) -> str:  # 为指定会话申请下一个任务 ID。
        """获取下一个递增任务 ID。"""  # 说明 ID 由 Redis INCR 保证单调递增。
        counter_key = self._task_counter_key(session_id)  # 计算当前会话的计数器 key。
        next_value = await self._redis.incr(counter_key)  # 使用 INCR 原子递增并获取新值。
        return str(next_value)  # 将自增值转换为字符串任务 ID 返回。

    async def create_task(self, session_id: str, task: TaskItem) -> TaskItem:  # 创建单个任务记录。
        """创建任务记录。"""  # 说明创建操作会直接写入 Redis 哈希。
        tasks_key = self._tasks_key(session_id)  # 计算任务哈希 key。
        task_json = json.dumps(task.model_dump(mode="json"), ensure_ascii=False)  # 序列化任务对象为 JSON。
        await self._redis.hset(tasks_key, task.id, task_json)  # 按任务 ID 写入 Redis 哈希字段。
        return task  # 返回原任务对象，方便调用方继续链式处理。

    async def get_task(self, session_id: str, task_id: str) -> TaskItem | None:  # 读取单个任务记录。
        """读取指定任务。"""  # 说明任务不存在时返回 None。
        tasks_key = self._tasks_key(session_id)  # 计算任务哈希 key。
        task_json = await self._redis.hget(tasks_key, task_id)  # 从 Redis 哈希中读取指定任务字段。
        if task_json is None:  # 如果字段不存在，说明任务尚未创建或已删除。
            logger.debug("任务不存在: session_id=%s, task_id=%s", session_id, task_id)  # 记录调试日志。
            return None  # 明确返回 None，供上层判断不存在场景。
        return TaskItem.model_validate(json.loads(task_json))  # 反序列化 JSON 并校验为任务模型。

    async def list_tasks(self, session_id: str) -> list[TaskItem]:  # 列出会话下所有任务记录。
        """列出指定会话的所有任务。"""  # 说明返回值不保证排序，由上层统一排序。
        tasks_key = self._tasks_key(session_id)  # 计算任务哈希 key。
        task_mapping = await self._redis.hgetall(tasks_key)  # 读取整个会话任务哈希。
        tasks: list[TaskItem] = []  # 初始化任务列表容器，用于承接反序列化结果。
        for task_json in task_mapping.values():  # 遍历每个任务的 JSON 字符串值。
            tasks.append(TaskItem.model_validate(json.loads(task_json)))  # 反序列化后追加到任务列表。
        return tasks  # 返回当前会话下的所有任务对象。

    async def save_task(self, session_id: str, task: TaskItem) -> TaskItem:  # 保存任务的最新状态。
        """覆盖保存指定任务。"""  # 说明更新与创建都走同一序列化格式。
        tasks_key = self._tasks_key(session_id)  # 计算任务哈希 key。
        task_json = json.dumps(task.model_dump(mode="json"), ensure_ascii=False)  # 序列化任务对象为 JSON。
        await self._redis.hset(tasks_key, task.id, task_json)  # 覆盖写入当前任务字段。
        return task  # 返回保存后的任务对象。

    async def delete_task(self, session_id: str, task_id: str) -> bool:  # 删除指定任务记录。
        """删除指定任务。"""  # 说明删除成功返回 True，任务不存在返回 False。
        tasks_key = self._tasks_key(session_id)  # 计算任务哈希 key。
        deleted_count = await self._redis.hdel(tasks_key, task_id)  # 删除指定任务字段并获取删除条数。
        return deleted_count > 0  # 将删除条数转换为布尔值返回。
