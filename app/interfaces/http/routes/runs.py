"""运行查询路由。

处理 GET /runs/{run_id} 接口。
"""

from __future__ import annotations

import logging
from fastapi import APIRouter, Depends

from app.core.models.error import AppError, ErrorCode
from app.core.models.run import Run, RunStatus
from app.interfaces.http.dependencies import get_run_control_service
from app.interfaces.http.schemas.run import CancelRunResponse, GetRunResponse
from app.interfaces.http.schemas.common import (
    RequestFailedResponse,
)
from app.services.run_control_service import RunControlService

logger = logging.getLogger(__name__)


router = APIRouter()


def _build_get_run_response(run: Run) -> GetRunResponse:
    """把运行领域模型转换为查询运行响应。

    字段字符串化只属于 HTTP 表示层，不应下沉到业务层。
    """
    return GetRunResponse(
        run_id=run.run_id,
        session_id=run.session_id,
        status=run.status.value,
        created_at=run.created_at.isoformat(),
        finished_at=run.finished_at.isoformat() if run.finished_at is not None else None,
        output=run.output,
        error_code=run.error_code,
        error_message=run.error_message,
    )


@router.get(
    "/runs/{run_id}",
    summary="查询运行详情",
)
async def get_run(
    run_id: str,
    run_control_service: RunControlService = Depends(get_run_control_service),
):
    """查询指定运行的详情。

    如果运行存在，返回包含 run_id、session_id、status 等字段的响应。
    如果运行不存在，返回 HTTP 200 + request_failed body。
    """
    logger.debug("查询运行请求: run_id=%s", run_id)

    run_result = await run_control_service.get_run(run_id)

    if isinstance(run_result, AppError):
        logger.error("运行不存在: run_id=%s", run_id)
        return RequestFailedResponse(
            error_code=ErrorCode.RUN_NOT_FOUND,
            message=f"Run {run_id} not found",
        )

    logger.debug("查询运行成功: run_id=%s, status=%s", run_id, run_result.status.value)
    return _build_get_run_response(run_result)


@router.post(
    "/runs/{run_id}/cancel",
    summary="取消运行",
)
async def cancel_run(
    run_id: str,
    run_control_service: RunControlService = Depends(get_run_control_service),
):
    """主动取消指定运行。"""
    logger.info("取消运行请求: run_id=%s", run_id)

    run_result = await run_control_service.get_run(run_id)

    if isinstance(run_result, AppError):
        logger.error("取消运行失败，运行不存在: run_id=%s", run_id)
        return RequestFailedResponse(
            error_code=ErrorCode.RUN_NOT_FOUND,
            message=f"Run {run_id} not found",
        )

    if run_result.status != RunStatus.RUNNING:
        logger.warning("取消运行失败，运行不在进行中: run_id=%s, status=%s", run_id, run_result.status.value)
        return RequestFailedResponse(
            error_code=ErrorCode.RUN_NOT_FOUND,
            message=f"Run {run_id} is not running",
        )

    cancelled = run_control_service.cancel_run(run_id)
    logger.info("取消运行成功: run_id=%s, cancelled=%s", run_id, cancelled)
    return CancelRunResponse(run_id=run_id, cancelled=cancelled)
