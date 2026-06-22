"""任务业务服务。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from app.core.models.task import TaskItem, TaskStatus

if TYPE_CHECKING:
    from app.core.ports.stores import TaskStore


def _to_camel_task(task: TaskItem) -> dict[str, Any]:
    """将 TaskItem 转换为对外暴露的完整 camelCase 字典。"""
    return {
        "id": task.id,
        "subject": task.subject,
        "description": task.description,
        "status": task.status.value,
        "activeForm": task.active_form,
        "owner": task.owner,
        "metadata": task.metadata,
        "blocks": task.blocks,
        "blockedBy": task.blocked_by,
    }


def _to_camel_summary(task: TaskItem) -> dict[str, Any]:
    """将 TaskItem 转换为对外暴露的摘要 camelCase 字典。"""
    return {
        "id": task.id,
        "subject": task.subject,
        "status": task.status.value,
        "owner": task.owner,
        "blockedBy": task.blocked_by,
    }


class TaskService:
    """任务业务服务。"""

    def __init__(self, task_store: "TaskStore") -> None:
        self._task_store = task_store

    async def create_task(
        self,
        session_id: str,
        subject: str,
        description: str,
        active_form: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """创建新任务。"""
        task_id = await self._task_store.next_task_id(session_id)
        task = TaskItem(
            id=task_id,
            subject=subject,
            description=description,
            active_form=active_form,
            status=TaskStatus.PENDING,
            owner=None,
            metadata=metadata or {},
            blocks=[],
            blocked_by=[],
        )
        await self._task_store.create_task(session_id, task)
        return json.dumps(_to_camel_task(task), ensure_ascii=False)

    async def get_task(self, session_id: str, task_id: str) -> str | None:
        """获取指定任务详情。"""
        task = await self._task_store.get_task(session_id, task_id)
        if task is None:
            return None
        return json.dumps(_to_camel_task(task), ensure_ascii=False)

    async def list_tasks(self, session_id: str) -> str:
        """列出当前会话的所有任务摘要。"""
        tasks = await self._task_store.list_tasks(session_id)
        tasks.sort(key=lambda t: int(t.id))
        summaries = [_to_camel_summary(t) for t in tasks]
        return json.dumps(summaries, ensure_ascii=False)

    async def update_task(
        self,
        session_id: str,
        task_id: str,
        subject: str | None = None,
        description: str | None = None,
        active_form: str | None = None,
        status: str | None = None,
        owner: str | None = None,
        metadata: dict[str, Any] | None = None,
        add_blocks: list[str] | None = None,
        add_blocked_by: list[str] | None = None,
    ) -> str | None:
        """更新任务信息。"""
        task = await self._task_store.get_task(session_id, task_id)
        if task is None:
            return None

        # 处理物理删除语义
        if status == "deleted":
            await self._task_store.delete_task(session_id, task_id)
            all_tasks = await self._task_store.list_tasks(session_id)
            for other in all_tasks:
                modified = False
                if task_id in other.blocks:
                    other.blocks.remove(task_id)
                    modified = True
                if task_id in other.blocked_by:
                    other.blocked_by.remove(task_id)
                    modified = True
                if modified:
                    await self._task_store.save_task(session_id, other)
            return json.dumps({"taskId": task_id, "deleted": True}, ensure_ascii=False)

        # 普通字段更新
        if subject is not None:
            task.subject = subject
        if description is not None:
            task.description = description
        if active_form is not None:
            task.active_form = active_form
        if owner is not None:
            task.owner = owner
        if status is not None:
            task.status = TaskStatus(status)

        # metadata 合并语义：值为 null 时删除对应键
        if metadata is not None:
            for key, value in metadata.items():
                if value is None:
                    task.metadata.pop(key, None)
                else:
                    task.metadata[key] = value

        # 依赖追加与双向一致性维护
        if add_blocks:
            new_blocks = [bid for bid in add_blocks if bid not in task.blocks]
            for bid in new_blocks:
                if bid == task_id:
                    raise ValueError(f"任务不能依赖自身: {task_id}")
                target = await self._task_store.get_task(session_id, bid)
                if target is None:
                    raise ValueError(f"依赖的任务不存在: {bid}")
                task.blocks.append(bid)
                if task_id not in target.blocked_by:
                    target.blocked_by.append(task_id)
                    await self._task_store.save_task(session_id, target)

        if add_blocked_by:
            new_blocked_by = [bid for bid in add_blocked_by if bid not in task.blocked_by]
            for bid in new_blocked_by:
                if bid == task_id:
                    raise ValueError(f"任务不能依赖自身: {task_id}")
                target = await self._task_store.get_task(session_id, bid)
                if target is None:
                    raise ValueError(f"依赖的任务不存在: {bid}")
                task.blocked_by.append(bid)
                if task_id not in target.blocks:
                    target.blocks.append(task_id)
                    await self._task_store.save_task(session_id, target)

        await self._task_store.save_task(session_id, task)
        return json.dumps(_to_camel_task(task), ensure_ascii=False)
