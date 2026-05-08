"""任务模型单元测试。"""

from __future__ import annotations  # 启用未来注解

import pytest  # 导入 pytest 测试框架
from pydantic import ValidationError  # 导入 Pydantic 验证错误

from app.core.models.task import TaskItem, TaskStatus  # 导入任务模型


def test_task_status_values():  # 测试任务状态枚举值
    """TaskStatus 应包含预期的三个状态值。"""
    assert TaskStatus.PENDING.value == "pending"  # 验证pending状态值
    assert TaskStatus.IN_PROGRESS.value == "in_progress"  # 验证in_progress状态值
    assert TaskStatus.COMPLETED.value == "completed"  # 验证completed状态值


def test_task_item_defaults():  # 测试 TaskItem 默认值
    """TaskItem 在仅提供必填字段时，默认值应正确初始化。"""
    task = TaskItem(id="1", subject="测试任务", description="这是一个测试任务")  # 创建最小任务对象
    assert task.status == TaskStatus.PENDING  # 默认状态应为pending
    assert task.active_form is None  # active_form默认应为None
    assert task.owner is None  # owner默认应为None
    assert task.metadata == {}  # metadata默认应为空字典
    assert task.blocks == []  # blocks默认应为空列表
    assert task.blocked_by == []  # blocked_by默认应为空列表


def test_task_item_serialization():  # 测试 TaskItem 序列化
    """TaskItem 应能正确序列化为字典并反序列化。"""
    task = TaskItem(
        id="1",
        subject="测试任务",
        description="这是一个测试任务",
        active_form="正在测试",
        status=TaskStatus.IN_PROGRESS,
        owner="agent-1",
        metadata={"priority": "high"},
        blocks=["2"],
        blocked_by=["3"],
    )  # 创建一个完整字段的任务对象
    data = task.model_dump(mode="json")  # 序列化为JSON兼容字典
    assert data["status"] == "in_progress"  # 验证状态序列化正确
    assert data["active_form"] == "正在测试"  # 验证active_form序列化正确
    assert data["metadata"] == {"priority": "high"}  # 验证metadata序列化正确

    restored = TaskItem.model_validate(data)  # 从字典反序列化回对象
    assert restored.id == task.id  # 验证id一致
    assert restored.status == task.status  # 验证状态一致
    assert restored.blocks == task.blocks  # 验证blocks一致


def test_task_item_id_required():  # 测试 id 必填校验
    """TaskItem 的 id 不能为空字符串。"""
    with pytest.raises(ValidationError):  # 预期抛出验证错误
        TaskItem(id="", subject="测试任务", description="这是一个测试任务")
