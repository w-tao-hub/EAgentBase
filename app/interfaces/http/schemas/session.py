"""会话相关的 HTTP 请求/响应 Schema 定义。"""

from __future__ import annotations  # 启用未来注解

from typing import Optional  # 导入可选类型

from pydantic import BaseModel  # 导入 Pydantic 基础模型


class CreateSessionResponse(BaseModel):
    """创建会话成功响应模型。

    当 POST /sessions 成功创建新会话时返回该模型。
    """

    # 新创建的会话唯一标识
    session_id: str

    # 会话绑定的 Agent 标识
    agent_id: str

    # 会话创建时间（ISO 格式字符串）
    created_at: str


class GetSessionResponse(BaseModel):
    """查询会话详情响应模型。

    当 GET /sessions/{session_id} 成功找到会话时返回该模型。
    """

    # 会话唯一标识
    session_id: str

    # 会话绑定的 Agent 标识
    agent_id: str

    # 会话创建时间（ISO 格式字符串）
    created_at: str

    # 当前会话中的消息数量
    message_count: int

    # 当前活跃的 Run ID，如果没有则为 None
    active_run_id: Optional[str] = None
