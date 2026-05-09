"""通用 HTTP 响应 Schema 定义。

包含 request_failed 等通用错误响应模型。
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from app.core.models.error import ErrorCode


class RequestFailedResponse(BaseModel):
    """请求失败响应模型。"""

    type: str = "request_failed"
    error_code: ErrorCode
    message: str
    run_id: Optional[str] = None
