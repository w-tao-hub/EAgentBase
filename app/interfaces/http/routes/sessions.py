"""会话路由。

处理 POST /sessions 和 GET /sessions/{session_id} 接口。
"""

from __future__ import annotations

import logging
from fastapi import APIRouter, Body, Depends

from app.core.models.session import Session
from app.interfaces.http.dependencies import get_session_service
from app.services.session_service import SessionView
from app.services.session_service import SessionService
from app.interfaces.http.schemas.session import (
    CreateSessionRequest,
    CreateSessionResponse,
    GetSessionResponse,
)
from app.interfaces.http.schemas.common import (
    RequestFailedResponse,
)
from app.core.models.error import ErrorCode

logger = logging.getLogger(__name__)


router = APIRouter()


def _build_create_session_response(session: Session) -> CreateSessionResponse:
    """把会话领域模型转换为创建会话响应。

    序列化属于 HTTP 表示层职责，统一收口在 Route 内部。
    """
    return CreateSessionResponse(
        session_id=session.session_id,
        agent_id=session.agent_id,
        created_at=session.created_at.isoformat(),
    )


def _build_get_session_response(view: SessionView) -> GetSessionResponse:
    """把会话视图转换为查询会话响应。

    这里仅负责 HTTP 层字段映射，不承载业务编排逻辑。
    """
    return GetSessionResponse(
        session_id=view.session_id,
        agent_id=view.agent_id,
        created_at=view.created_at.isoformat(),
        message_count=view.message_count,
        active_run_id=view.active_run_id,
    )


@router.post(
    "/sessions",
    response_model=CreateSessionResponse | RequestFailedResponse,
    summary="创建新会话",
)
async def create_session(
    payload: CreateSessionRequest | None = Body(default=None),
    session_service: SessionService = Depends(get_session_service),
):
    """创建新的对话会话。"""
    master_agent_name = payload.master_agent_name if payload is not None else None  # 提取主代理名称
    try:
        session = await session_service.create_session(master_agent_name=master_agent_name)  # 创建会话
    except ValueError as error:
        # 仅把“未知主代理”映射为 request_failed，避免把其他实现错误误报成业务输入问题。
        if not str(error).startswith(f"{ErrorCode.UNKNOWN_MASTER_AGENT.value}:"):
            raise
        return RequestFailedResponse(
            error_code=ErrorCode.UNKNOWN_MASTER_AGENT,
            message=str(error),
        )
    logger.info("会话创建成功: session_id=%s, agent_id=%s", session.session_id, session.agent_id)

    return _build_create_session_response(session)


@router.get(
    "/sessions/{session_id}",
    summary="查询会话详情",
)
async def get_session(
    session_id: str,
    session_service: SessionService = Depends(get_session_service),
):
    """查询指定会话的详情。"""
    logger.debug("查询会话请求: session_id=%s", session_id)

    session_view = await session_service.get_session_view(session_id)

    if session_view is None:
        logger.error("会话不存在: session_id=%s", session_id)
        return RequestFailedResponse(
            error_code=ErrorCode.SESSION_NOT_FOUND,
            message=f"Session {session_id} not found",
        )

    logger.debug("查询会话成功: session_id=%s, message_count=%d", session_id, session_view.message_count)
    return _build_get_session_response(session_view)
