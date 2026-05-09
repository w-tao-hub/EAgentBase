"""运行查询相关的 HTTP 响应 Schema 定义。"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from app.core.models.error import ErrorCode


class GetRunResponse(BaseModel):
    """查询运行详情响应模型。"""

    run_id: str
    session_id: str
    status: str
    created_at: str
    finished_at: Optional[str] = None
    output: Optional[str] = None
    error_code: Optional[ErrorCode] = None
    error_message: Optional[str] = None


class CancelRunResponse(BaseModel):
    """取消运行响应模型。"""

    run_id: str
    cancelled: bool
