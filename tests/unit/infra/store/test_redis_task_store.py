"""RedisTaskStore 单元测试。"""

from __future__ import annotations  # 启用未来注解

import pytest  # 导入 pytest 测试框架

from app.core.models.task import TaskItem, TaskStatus  # 导入任务模型
from app.infra.store.redis_task_store import RedisTaskStore  # 导入任务存储


@pytest.fixture  # 定义测试夹具
async def task_store(fake_redis):
    """为每个测试提供一个独立的 RedisTaskStore 实例。"""
    return RedisTaskStore(fake_redis, key_prefix="test")  # 使用独立前缀避免冲突


class TestNextTaskId:
    """测试 next_task_id 行为。"""

    async def test_increments_within_session(self, task_store):  # 测试同一会话内递增
        """同一 session 内任务 ID 应单调递增。"""
        id1 = await task_store.next_task_id("session-a")  # 获取第一个ID
        id2 = await task_store.next_task_id("session-a")  # 获取第二个ID
        id3 = await task_store.next_task_id("session-a")  # 获取第三个ID
        assert id1 == "1"  # 第一次应从1开始
        assert id2 == "2"  # 第二次递增
        assert id3 == "3"  # 第三次递增

    async def test_isolation_across_sessions(self, task_store):  # 测试跨会话隔离
        """不同 session 的计数器应相互隔离。"""
        id_a = await task_store.next_task_id("session-a")  # session-a第一次
        id_b = await task_store.next_task_id("session-b")  # session-b第一次
        assert id_a == "1"  # session-a从1开始
        assert id_b == "1"  # session-b也从1开始，互不影响


class TestCrud:
    """测试基本 CRUD 操作。"""

    async def test_create_and_get(self, task_store):  # 测试创建与读取
        """创建任务后应能通过 get_task 正确读取。"""
        task = TaskItem(id="1", subject="测试", description="描述")  # 构造任务
        await task_store.create_task("session-a", task)  # 创建任务

        fetched = await task_store.get_task("session-a", "1")  # 读取任务
        assert fetched is not None  # 应存在
        assert fetched.subject == "测试"  # 验证标题一致
        assert fetched.status == TaskStatus.PENDING  # 验证状态一致

    async def test_get_missing_returns_none(self, task_store):  # 测试读取不存在的任务
        """读取不存在的任务应返回 None。"""
        result = await task_store.get_task("session-a", "99")  # 读取不存在的任务
        assert result is None  # 应返回None

    async def test_save_and_list(self, task_store):  # 测试保存与列表
        """保存更新后 list_tasks 应反映最新状态。"""
        task = TaskItem(id="1", subject="原标题", description="描述")  # 构造初始任务
        await task_store.create_task("session-a", task)  # 创建任务

        task.subject = "新标题"  # 修改标题
        task.status = TaskStatus.COMPLETED  # 修改状态
        await task_store.save_task("session-a", task)  # 保存更新

        tasks = await task_store.list_tasks("session-a")  # 列出任务
        assert len(tasks) == 1  # 应只有一个任务
        assert tasks[0].subject == "新标题"  # 验证标题已更新
        assert tasks[0].status == TaskStatus.COMPLETED  # 验证状态已更新

    async def test_delete_existing(self, task_store):  # 测试删除存在的任务
        """删除已存在的任务应返回 True，且后续无法读取。"""
        task = TaskItem(id="1", subject="测试", description="描述")  # 构造任务
        await task_store.create_task("session-a", task)  # 创建任务

        deleted = await task_store.delete_task("session-a", "1")  # 删除任务
        assert deleted is True  # 删除成功

        fetched = await task_store.get_task("session-a", "1")  # 再次读取
        assert fetched is None  # 应已不存在

    async def test_delete_missing_returns_false(self, task_store):  # 测试删除不存在的任务
        """删除不存在的任务应返回 False。"""
        deleted = await task_store.delete_task("session-a", "99")  # 删除不存在的任务
        assert deleted is False  # 应返回False

    async def test_list_isolation(self, task_store):  # 测试列表的会话隔离
        """list_tasks 应只返回当前会话的任务。"""
        await task_store.create_task(
            "session-a", TaskItem(id="1", subject="A任务", description="A")
        )  # 为session-a创建任务
        await task_store.create_task(
            "session-b", TaskItem(id="1", subject="B任务", description="B")
        )  # 为session-b创建任务

        tasks_a = await task_store.list_tasks("session-a")  # 列出session-a的任务
        tasks_b = await task_store.list_tasks("session-b")  # 列出session-b的任务

        assert len(tasks_a) == 1  # session-a只有一个任务
        assert tasks_a[0].subject == "A任务"  # 验证任务内容隔离
        assert len(tasks_b) == 1  # session-b只有一个任务
        assert tasks_b[0].subject == "B任务"  # 验证任务内容隔离
