"""任务领域模型定义。"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    """任务状态枚举。"""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class TaskItem(BaseModel):
    """表示单个会话内的任务项。"""

    id: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    description: str = Field(min_length=1)
    active_form: str | None = None
    status: TaskStatus = TaskStatus.PENDING
    owner: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    blocks: list[str] = Field(default_factory=list)
    blocked_by: list[str] = Field(default_factory=list)
