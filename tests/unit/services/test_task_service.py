"""TaskService 单元测试。"""

from __future__ import annotations  # 启用未来注解

import json  # 导入 JSON 模块

import pytest  # 导入 pytest 测试框架

from app.infra.store.redis_task_store import RedisTaskStore  # 导入任务存储
from app.services.task_service import TaskService  # 导入任务服务


@pytest.fixture  # 定义测试夹具
async def task_service(fake_redis):
    """为每个测试提供独立的 TaskService 实例。"""
    store = RedisTaskStore(fake_redis, key_prefix="test")  # 创建独立前缀的存储
    return TaskService(store)  # 创建任务服务


class TestCreateTask:
    """测试任务创建。"""

    async def test_create_returns_camel_case_json(self, task_service):  # 测试创建返回 camelCase
        """create_task 应返回完整的 camelCase JSON。"""
        result = await task_service.create_task(
            session_id="s1",
            subject="测试任务",
            description="描述",
            active_form="正在测试",
        )
        data = json.loads(result)  # 解析返回的 JSON
        assert data["id"] == "1"  # ID 从 1 开始
        assert data["subject"] == "测试任务"
        assert data["description"] == "描述"
        assert data["activeForm"] == "正在测试"
        assert data["status"] == "pending"
        assert data["owner"] is None
        assert data["metadata"] == {}
        assert data["blocks"] == []
        assert data["blockedBy"] == []

    async def test_ids_increment_per_session(self, task_service):  # 测试同一会话 ID 递增
        """同一 session 的任务 ID 应单调递增。"""
        r1 = await task_service.create_task("s1", "任务1", "描述1")
        r2 = await task_service.create_task("s1", "任务2", "描述2")
        assert json.loads(r1)["id"] == "1"
        assert json.loads(r2)["id"] == "2"

    async def test_missing_active_form_allowed(self, task_service):  # 测试 activeForm 缺失可创建
        """未提供 active_form 时仍可成功创建任务。"""
        result = await task_service.create_task("s1", "任务", "描述")
        data = json.loads(result)
        assert data["activeForm"] is None


class TestListTasks:
    """测试任务列表查询。"""

    async def test_sort_by_numeric_id(self, task_service):  # 测试按数字 ID 排序
        """list_tasks 应按数字 ID 升序返回。"""
        await task_service.create_task("s1", "任务3", "描述")
        await task_service.create_task("s1", "任务1", "描述")
        await task_service.create_task("s1", "任务2", "描述")

        result = await task_service.list_tasks("s1")
        tasks = json.loads(result)
        ids = [t["id"] for t in tasks]
        assert ids == ["1", "2", "3"]  # 验证升序

    async def test_returns_summary_only(self, task_service):  # 测试只返回摘要字段
        """list_tasks 返回的字段应仅为摘要字段。"""
        await task_service.create_task("s1", "任务", "描述", active_form="正在做")
        result = await task_service.list_tasks("s1")
        task = json.loads(result)[0]
        assert set(task.keys()) == {"id", "subject", "status", "owner", "blockedBy"}
        assert "description" not in task
        assert "activeForm" not in task
        assert "metadata" not in task
        assert "blocks" not in task


class TestGetTask:
    """测试任务获取。"""

    async def test_get_existing(self, task_service):  # 测试获取存在的任务
        """能正确返回已创建任务的完整 JSON。"""
        await task_service.create_task("s1", "任务", "描述")
        result = await task_service.get_task("s1", "1")
        assert json.loads(result)["subject"] == "任务"

    async def test_get_missing_returns_none(self, task_service):  # 测试获取不存在的任务
        """获取不存在的任务应返回 None。"""
        result = await task_service.get_task("s1", "99")
        assert result is None


class TestUpdateTask:
    """测试任务更新。"""

    async def test_update_fields(self, task_service):  # 测试字段更新
        """应能更新 subject、description、active_form、owner、status。"""
        await task_service.create_task("s1", "原题", "原描述")
        result = await task_service.update_task(
            session_id="s1",
            task_id="1",
            subject="新题",
            description="新描述",
            active_form="正在更新",
            status="in_progress",
            owner="agent-a",
        )
        data = json.loads(result)
        assert data["subject"] == "新题"
        assert data["description"] == "新描述"
        assert data["activeForm"] == "正在更新"
        assert data["status"] == "in_progress"
        assert data["owner"] == "agent-a"

    async def test_metadata_merge(self, task_service):  # 测试 metadata 合并
        """metadata 应执行 merge，值为 null 时删除键。"""
        await task_service.create_task("s1", "任务", "描述", metadata={"a": 1, "b": 2})
        result = await task_service.update_task(
            session_id="s1",
            task_id="1",
            metadata={"b": None, "c": 3},
        )
        data = json.loads(result)
        assert data["metadata"] == {"a": 1, "c": 3}  # b 被删除，c 被新增

    async def test_update_missing_returns_none(self, task_service):  # 测试更新不存在的任务
        """更新不存在的任务应返回 None。"""
        result = await task_service.update_task("s1", "99", subject="新题")
        assert result is None


class TestDeleteTask:
    """测试删除语义。"""

    async def test_physical_delete_and_cleanup_refs(self, task_service):  # 测试物理删除与级联清理
        """status='deleted' 应物理删除任务并清理其他任务中的反向引用。"""
        await task_service.create_task("s1", "任务1", "描述")
        await task_service.create_task("s1", "任务2", "描述")
        await task_service.create_task("s1", "任务3", "描述")

        # 建立依赖链：1 -> 2 -> 3
        await task_service.update_task("s1", "1", add_blocks=["2"])
        await task_service.update_task("s1", "2", add_blocks=["3"])

        result = await task_service.update_task("s1", "2", status="deleted")
        data = json.loads(result)
        assert data == {"taskId": "2", "deleted": True}  # 验证删除确认结构

        # 验证任务 2 已不存在
        assert await task_service.get_task("s1", "2") is None

        # 验证任务 1 的 blocks 被清理
        t1 = json.loads(await task_service.get_task("s1", "1"))
        assert t1["blocks"] == []
        # 验证任务 3 的 blocked_by 被清理
        t3 = json.loads(await task_service.get_task("s1", "3"))
        assert t3["blockedBy"] == []


class TestDependencyConsistency:
    """测试依赖双向一致性。"""

    async def test_add_blocks_updates_blocked_by(self, task_service):  # 测试 add_blocks 反向更新
        """任务 A 的 add_blocks=B 应自动更新 B 的 blocked_by=A。"""
        await task_service.create_task("s1", "A", "描述")
        await task_service.create_task("s1", "B", "描述")

        result = await task_service.update_task("s1", "1", add_blocks=["2"])
        data = json.loads(result)
        assert data["blocks"] == ["2"]

        b = json.loads(await task_service.get_task("s1", "2"))
        assert b["blockedBy"] == ["1"]  # 反向字段同步更新

    async def test_add_blocked_by_updates_blocks(self, task_service):  # 测试 add_blocked_by 反向更新
        """任务 A 的 add_blocked_by=B 应自动更新 B 的 blocks=A。"""
        await task_service.create_task("s1", "A", "描述")
        await task_service.create_task("s1", "B", "描述")

        result = await task_service.update_task("s1", "1", add_blocked_by=["2"])
        data = json.loads(result)
        assert data["blockedBy"] == ["2"]

        b = json.loads(await task_service.get_task("s1", "2"))
        assert b["blocks"] == ["1"]  # 反向字段同步更新

    async def test_duplicate_dependency_ignored(self, task_service):  # 测试重复依赖被去重
        """重复追加同一依赖应被自动去重，不报错。"""
        await task_service.create_task("s1", "A", "描述")
        await task_service.create_task("s1", "B", "描述")

        await task_service.update_task("s1", "1", add_blocks=["2"])
        await task_service.update_task("s1", "1", add_blocks=["2"])  # 再次追加同一依赖

        a = json.loads(await task_service.get_task("s1", "1"))
        assert a["blocks"] == ["2"]  # 应无重复

    async def test_self_dependency_raises(self, task_service):  # 测试自依赖报错
        """add_blocks 或 add_blocked_by 包含自身 ID 时应抛出 ValueError。"""
        await task_service.create_task("s1", "A", "描述")
        with pytest.raises(ValueError, match="任务不能依赖自身"):
            await task_service.update_task("s1", "1", add_blocks=["1"])
        with pytest.raises(ValueError, match="任务不能依赖自身"):
            await task_service.update_task("s1", "1", add_blocked_by=["1"])

    async def test_dependency_missing_task_raises(self, task_service):  # 测试依赖不存在的任务报错
        """add_blocks 或 add_blocked_by 引用不存在的任务时应抛出 ValueError。"""
        await task_service.create_task("s1", "A", "描述")
        with pytest.raises(ValueError, match="依赖的任务不存在"):
            await task_service.update_task("s1", "1", add_blocks=["99"])
        with pytest.raises(ValueError, match="依赖的任务不存在"):
            await task_service.update_task("s1", "1", add_blocked_by=["99"])
