"""Session 领域模型定义。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class Session(BaseModel):
    """表示一次用户与 Agent 的对话会话。

    Session 负责绑定 Agent，并为后续消息和 Run 提供上下文范围。
    """

    # 会话的唯一标识符，用于定位具体会话
    session_id: str = Field(min_length=1)

    # 当前会话所绑定的 Agent 的标识符
    agent_id: str = Field(min_length=1)

    # 会话创建时间，使用 datetime 类型以支持运行时和存储层的灵活转换
    created_at: datetime
