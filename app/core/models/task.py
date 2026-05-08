"""任务领域模型定义。"""  # 模块级说明，声明当前文件负责任务模型。

from __future__ import annotations  # 启用未来注解，避免类型前向引用问题。

from enum import Enum  # 导入枚举基类，用于定义任务状态。
from typing import Any  # 导入任意类型，用于描述 metadata 的值类型。

from pydantic import BaseModel, Field  # 导入 Pydantic 基类和字段声明工具。


class TaskStatus(str, Enum):  # 定义任务状态枚举，统一状态字符串常量。
    """任务状态枚举。"""  # 说明该枚举只承载持久化中的常规状态。

    PENDING = "pending"  # 表示任务尚未开始处理。
    IN_PROGRESS = "in_progress"  # 表示任务正在处理过程中。
    COMPLETED = "completed"  # 表示任务已完整处理完成。


class TaskItem(BaseModel):  # 定义任务实体模型，承载会话级任务完整信息。
    """表示单个会话内的任务项。"""  # 说明该模型对应任务工具返回的完整内部实体。

    id: str = Field(min_length=1)  # 任务唯一标识，按会话内递增字符串编号生成。
    subject: str = Field(min_length=1)  # 任务标题，面向执行的祈使句短标题。
    description: str = Field(min_length=1)  # 任务详细描述，包含上下文和验收标准。
    active_form: str | None = None  # 任务进行中时显示的现在进行时文案，可为空。
    status: TaskStatus = TaskStatus.PENDING  # 任务当前状态，默认从 pending 开始。
    owner: str | None = None  # 任务负责人标识，未认领时为空。
    metadata: dict[str, Any] = Field(default_factory=dict)  # 任务附加元数据，默认空字典。
    blocks: list[str] = Field(default_factory=list)  # 当前任务完成后才能继续的下游任务 ID 列表。
    blocked_by: list[str] = Field(default_factory=list)  # 当前任务启动前必须先完成的前置任务 ID 列表。
