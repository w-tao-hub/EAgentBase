"""RedisTaskStore 实现。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from app.core.models.task import TaskItem
from app.infra.logging import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    from redis.asyncio import Redis


class RedisTaskStore:
    """基于 Redis 的任务存储。"""

    def __init__(self, redis: Redis, key_prefix: str = "agent") -> None:
        self._redis = redis
        self._key_prefix = key_prefix

    def _tasks_key(self, session_id: str) -> str:
        return f"{self._key_prefix}:tasks:{session_id}"

    def _task_counter_key(self, session_id: str) -> str:
        return f"{self._key_prefix}:task_counter:{session_id}"

    async def next_task_id(self, session_id: str) -> str:
        counter_key = self._task_counter_key(session_id)
        next_value = await self._redis.incr(counter_key)
        return str(next_value)

    async def create_task(self, session_id: str, task: TaskItem) -> TaskItem:
        tasks_key = self._tasks_key(session_id)
        task_json = json.dumps(task.model_dump(mode="json"), ensure_ascii=False)
        await self._redis.hset(tasks_key, task.id, task_json)
        return task

    async def get_task(self, session_id: str, task_id: str) -> TaskItem | None:
        tasks_key = self._tasks_key(session_id)
        task_json = await self._redis.hget(tasks_key, task_id)
        if task_json is None:
            logger.debug("任务不存在: session_id=%s, task_id=%s", session_id, task_id)
            return None
        return TaskItem.model_validate(json.loads(task_json))

    async def list_tasks(self, session_id: str) -> list[TaskItem]:
        tasks_key = self._tasks_key(session_id)
        task_mapping = await self._redis.hgetall(tasks_key)
        tasks: list[TaskItem] = []
        for task_json in task_mapping.values():
            tasks.append(TaskItem.model_validate(json.loads(task_json)))
        return tasks

    async def save_task(self, session_id: str, task: TaskItem) -> TaskItem:
        tasks_key = self._tasks_key(session_id)
        task_json = json.dumps(task.model_dump(mode="json"), ensure_ascii=False)
        await self._redis.hset(tasks_key, task.id, task_json)
        return task

    async def delete_task(self, session_id: str, task_id: str) -> bool:
        tasks_key = self._tasks_key(session_id)
        deleted_count = await self._redis.hdel(tasks_key, task_id)
        return deleted_count > 0
