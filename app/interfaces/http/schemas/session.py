"""会话相关的 HTTP 请求/响应 Schema 定义。"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class CreateSessionResponse(BaseModel):
    """创建会话成功响应模型。"""

    session_id: str
    agent_id: str
    created_at: str


class GetSessionResponse(BaseModel):
    """查询会话详情响应模型。"""

    session_id: str
    agent_id: str
    created_at: str
    message_count: int
    active_run_id: Optional[str] = None
