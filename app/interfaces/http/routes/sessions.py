"""会话路由。

处理 POST /sessions 和 GET /sessions/{session_id} 接口。
"""

from __future__ import annotations  # 启用未来注解

import logging  # 导入标准库日志模块，避免 interfaces 依赖 infra 包路径
from fastapi import APIRouter, Depends  # 导入 FastAPI 路由和依赖注入工具

from app.core.models.session import Session  # 导入会话领域模型
from app.interfaces.http.dependencies import get_session_service  # 导入会话服务依赖函数
from app.services.session_service import SessionView  # 导入会话视图模型
from app.services.session_service import SessionService  # 导入会话服务类型
from app.interfaces.http.schemas.session import (  # 导入会话 Schema
    CreateSessionResponse,
    GetSessionResponse,
)
from app.interfaces.http.schemas.common import (  # 导入通用错误响应
    RequestFailedResponse,
)
from app.core.models.error import ErrorCode  # 导入错误码枚举

# 获取模块级日志器。
# 直接使用标准库 logging，保持 interfaces 层不依赖 infra 包路径。
logger = logging.getLogger(__name__)


# 创建会话路由器
router = APIRouter()


def _build_create_session_response(session: Session) -> CreateSessionResponse:
    """把会话领域模型转换为创建会话响应。

    序列化属于 HTTP 表示层职责，统一收口在 Route 内部。
    """
    return CreateSessionResponse(
        session_id=session.session_id,  # 会话 ID
        agent_id=session.agent_id,  # Agent ID
        created_at=session.created_at.isoformat(),  # ISO 格式创建时间
    )


def _build_get_session_response(view: SessionView) -> GetSessionResponse:
    """把会话视图转换为查询会话响应。

    这里仅负责 HTTP 层字段映射，不承载业务编排逻辑。
    """
    return GetSessionResponse(
        session_id=view.session_id,  # 会话 ID
        agent_id=view.agent_id,  # Agent ID
        created_at=view.created_at.isoformat(),  # 创建时间
        message_count=view.message_count,  # 消息数量
        active_run_id=view.active_run_id,  # 活跃 Run ID
    )


@router.post(  # POST /sessions
    "/sessions",
    response_model=CreateSessionResponse,  # 响应模型
    summary="创建新会话",  # 接口摘要
)
async def create_session(
    session_service: SessionService = Depends(get_session_service),  # 注入会话服务
):  # 创建会话
    """创建新的对话会话。

    直接通过会话服务创建新会话，返回包含 session_id、agent_id 和 created_at 的响应。
    """
    logger.info("创建会话请求")

    # 直接通过注入的会话服务创建会话。
    # Route 只依赖会话能力本身，不再感知整个容器。
    session = await session_service.create_session()
    logger.info("会话创建成功: session_id=%s, agent_id=%s", session.session_id, session.agent_id)

    # 在 HTTP 层完成响应序列化
    return _build_create_session_response(session)


@router.get(  # GET /sessions/{session_id}
    "/sessions/{session_id}",
    summary="查询会话详情",  # 接口摘要
)
async def get_session(
    session_id: str,  # 会话 ID
    session_service: SessionService = Depends(get_session_service),  # 注入会话服务
):  # 查询会话
    """查询指定会话的详情。

    如果会话存在，返回包含 session_id、agent_id、created_at、message_count 和 active_run_id 的响应。
    如果会话不存在，返回 HTTP 200 + request_failed body。
    """
    logger.debug("查询会话请求: session_id=%s", session_id)

    # 直接通过注入的会话服务查询会话视图。
    # Route 只依赖会话能力本身，不再感知整个容器。
    session_view = await session_service.get_session_view(session_id)

    if session_view is None:  # 会话不存在
        logger.error("会话不存在: session_id=%s", session_id)
        # 返回业务错误（HTTP 200 + request_failed body）
        return RequestFailedResponse(
            error_code=ErrorCode.SESSION_NOT_FOUND,  # 错误码：会话不存在
            message=f"Session {session_id} not found",  # 错误消息
        )

    logger.debug("查询会话成功: session_id=%s, message_count=%d", session_id, session_view.message_count)
    # 在 HTTP 层完成响应序列化
    return _build_get_session_response(session_view)
