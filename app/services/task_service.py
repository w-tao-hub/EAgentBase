"""任务业务服务。

提供任务创建、查询、列表、更新和删除的完整业务逻辑，
集中处理依赖关系一致性、camelCase 序列化和删除级联清理。
"""

from __future__ import annotations  # 启用未来注解

import json  # 导入 JSON 模块用于序列化
from typing import Any  # 导入任意类型提示

from app.core.models.task import TaskItem, TaskStatus  # 导入任务模型和状态枚举
from app.infra.store.redis_task_store import RedisTaskStore  # 导入 Redis 任务存储


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
    """任务业务服务。

    封装 RedisTaskStore，负责任务的 CRUD、依赖双向一致性维护、
    删除级联清理以及 camelCase JSON 序列化。
    """

    def __init__(self, task_store: RedisTaskStore) -> None:
        """初始化任务服务。

        Args:
            task_store: Redis 任务存储实例。
        """
        self._task_store = task_store  # 保存任务存储实例

    async def create_task(
        self,
        session_id: str,
        subject: str,
        description: str,
        active_form: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """创建新任务。

        Args:
            session_id: 会话标识符。
            subject: 任务标题。
            description: 任务详细描述。
            active_form: 任务进行中显示的现在进行时文案。
            metadata: 附加元数据字典。

        Returns:
            完整任务对象的 camelCase JSON 字符串。
        """
        task_id = await self._task_store.next_task_id(session_id)  # 申请下一个递增任务 ID
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
        await self._task_store.create_task(session_id, task)  # 持久化到 Redis
        return json.dumps(_to_camel_task(task), ensure_ascii=False)  # 返回 camelCase JSON

    async def get_task(self, session_id: str, task_id: str) -> str | None:
        """获取指定任务详情。

        Args:
            session_id: 会话标识符。
            task_id: 任务标识符。

        Returns:
            完整任务对象的 camelCase JSON 字符串；若不存在则返回 None。
        """
        task = await self._task_store.get_task(session_id, task_id)  # 从 Redis 读取
        if task is None:
            return None  # 任务不存在
        return json.dumps(_to_camel_task(task), ensure_ascii=False)  # 返回 camelCase JSON

    async def list_tasks(self, session_id: str) -> str:
        """列出当前会话的所有任务摘要。

        Args:
            session_id: 会话标识符。

        Returns:
            按任务 ID 数字升序排列的摘要对象数组 JSON 字符串。
        """
        tasks = await self._task_store.list_tasks(session_id)  # 获取全部任务
        tasks.sort(key=lambda t: int(t.id))  # 按数字 ID 升序排序
        summaries = [_to_camel_summary(t) for t in tasks]  # 仅保留摘要字段
        return json.dumps(summaries, ensure_ascii=False)  # 返回 JSON 数组

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
        """更新任务信息。

        支持字段更新、状态变更、依赖追加和物理删除。

        Args:
            session_id: 会话标识符。
            task_id: 任务标识符。
            subject: 新标题。
            description: 新描述。
            active_form: 新现在进行时文案。
            status: 新状态，传入 "deleted" 时触发物理删除。
            owner: 新负责人。
            metadata: 要合并的元数据。
            add_blocks: 需等待此任务完成后才能开始的下游任务 ID 列表（追加）。
            add_blocked_by: 此任务开始前必须完成的前置任务 ID 列表（追加）。

        Returns:
            正常更新时返回完整任务 camelCase JSON；
            删除成功时返回 {"taskId": ..., "deleted": true}；
            任务不存在时返回 None。
        """
        task = await self._task_store.get_task(session_id, task_id)  # 读取当前任务
        if task is None:
            return None  # 任务不存在

        # 处理物理删除语义
        if status == "deleted":
            await self._task_store.delete_task(session_id, task_id)  # 删除当前任务
            all_tasks = await self._task_store.list_tasks(session_id)  # 获取会话下剩余任务
            for other in all_tasks:
                modified = False  # 标记是否需要保存
                if task_id in other.blocks:
                    other.blocks.remove(task_id)  # 清理 blocks 中的反向引用
                    modified = True
                if task_id in other.blocked_by:
                    other.blocked_by.remove(task_id)  # 清理 blocked_by 中的反向引用
                    modified = True
                if modified:
                    await self._task_store.save_task(session_id, other)  # 保存修改后的关联任务
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
            task.status = TaskStatus(status)  # 转换为枚举值

        # metadata 合并语义：值为 null 时删除对应键
        if metadata is not None:
            for key, value in metadata.items():
                if value is None:
                    task.metadata.pop(key, None)  # 删除键
                else:
                    task.metadata[key] = value  # 覆盖或新增

        # 依赖追加与双向一致性维护
        if add_blocks:
            # 去重并校验
            new_blocks = [bid for bid in add_blocks if bid not in task.blocks]
            for bid in new_blocks:
                if bid == task_id:
                    raise ValueError(f"任务不能依赖自身: {task_id}")  # 禁止自依赖
                target = await self._task_store.get_task(session_id, bid)
                if target is None:
                    raise ValueError(f"依赖的任务不存在: {bid}")  # 禁止引用不存在任务
                task.blocks.append(bid)  # 追加到当前任务的 blocks
                if task_id not in target.blocked_by:
                    target.blocked_by.append(task_id)  # 同步更新反向字段
                    await self._task_store.save_task(session_id, target)  # 保存反向关联任务

        if add_blocked_by:
            new_blocked_by = [bid for bid in add_blocked_by if bid not in task.blocked_by]
            for bid in new_blocked_by:
                if bid == task_id:
                    raise ValueError(f"任务不能依赖自身: {task_id}")  # 禁止自依赖
                target = await self._task_store.get_task(session_id, bid)
                if target is None:
                    raise ValueError(f"依赖的任务不存在: {bid}")  # 禁止引用不存在任务
                task.blocked_by.append(bid)  # 追加到当前任务的 blocked_by
                if task_id not in target.blocks:
                    target.blocks.append(task_id)  # 同步更新反向字段
                    await self._task_store.save_task(session_id, target)  # 保存反向关联任务

        await self._task_store.save_task(session_id, task)  # 保存更新后的当前任务
        return json.dumps(_to_camel_task(task), ensure_ascii=False)  # 返回 camelCase JSON
