"""聊天请求 Schema 定义。"""

from __future__ import annotations  # 启用未来注解

from typing import Optional  # 导入可选类型

from pydantic import BaseModel, Field  # 导入 Pydantic 基础模型和字段工具


class ChatRequest(BaseModel):
    """聊天请求模型。

    POST /chat 接口的请求体，包含会话 ID、用户消息和可选的元数据。
    """

    # 目标会话的唯一标识
    session_id: str = Field(min_length=1)

    # 用户输入的消息内容
    message: str = Field(min_length=1)

    # 可选的请求元数据，用于传递额外信息（不会传递给 Runtime）
    metadata: Optional[dict] = None
