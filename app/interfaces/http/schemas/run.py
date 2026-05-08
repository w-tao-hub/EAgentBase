"""运行查询相关的 HTTP 响应 Schema 定义。"""

from __future__ import annotations  # 启用未来注解

from typing import Optional  # 导入可选类型

from pydantic import BaseModel  # 导入 Pydantic 基础模型

from app.core.models.error import ErrorCode  # 导入错误码枚举


class GetRunResponse(BaseModel):
    """查询运行详情响应模型。

    当 GET /runs/{run_id} 成功找到运行记录时返回该模型。
    字段与 Run 领域模型一一对应，但使用字符串类型以简化 JSON 序列化。
    """

    # 运行唯一标识
    run_id: str

    # 运行所属的会话标识
    session_id: str

    # 运行当前状态（HTTP 响应使用字符串，避免直接暴露枚举对象）
    status: str

    # 运行创建时间（ISO 格式字符串）
    created_at: str

    # 运行结束时间，未完成时为 None
    finished_at: Optional[str] = None

    # 运行成功后的输出内容
    output: Optional[str] = None

    # 失败时的错误码
    error_code: Optional[ErrorCode] = None

    # 失败时的错误描述
    error_message: Optional[str] = None


class CancelRunResponse(BaseModel):
    """取消运行响应模型。

    当 POST /runs/{run_id}/cancel 成功发出取消信号时返回该模型。
    """

    # 被取消的运行唯一标识
    run_id: str

    # 是否已成功发出取消信号
    cancelled: bool
