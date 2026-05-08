"""通用 HTTP 响应 Schema 定义。

包含 request_failed 等通用错误响应模型。
"""

from __future__ import annotations  # 启用未来注解

from typing import Optional  # 导入可选类型

from pydantic import BaseModel  # 导入 Pydantic 基础模型

from app.core.models.error import ErrorCode  # 导入错误码枚举


class RequestFailedResponse(BaseModel):
    """请求失败响应模型。

    用于所有业务错误的统一响应格式，
    HTTP 状态码始终为 200，业务错误通过 body 中的 type 字段区分。
    """

    # 固定为 request_failed，标识这是一个业务错误响应
    type: str = "request_failed"

    # 错误码，使用 ErrorCode 枚举值
    error_code: ErrorCode

    # 错误的人类可读描述
    message: str

    # 可选的关联运行标识
    run_id: Optional[str] = None
