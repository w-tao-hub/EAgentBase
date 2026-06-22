"""聊天请求 Schema 定义。"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """聊天请求模型。"""

    session_id: str = Field(min_length=1)
    master_agent_name: str = Field(min_length=1)
    message: str = Field(min_length=1)
    metadata: Optional[dict] = None
