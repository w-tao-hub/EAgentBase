"""运行查询路由。

处理 GET /runs/{run_id} 接口。
"""

from __future__ import annotations  # 启用未来注解

import logging  # 导入标准库日志模块，避免 interfaces 依赖 infra 包路径
from fastapi import APIRouter, Depends  # 导入 FastAPI 路由和依赖注入工具

from app.core.models.error import AppError, ErrorCode  # 导入错误模型和错误码枚举
from app.core.models.run import Run, RunStatus  # 导入运行领域模型和状态枚举
from app.interfaces.http.dependencies import get_run_control_service  # 导入运行服务依赖函数
from app.interfaces.http.schemas.run import CancelRunResponse, GetRunResponse  # 导入运行响应 Schema
from app.interfaces.http.schemas.common import (  # 导入通用错误响应
    RequestFailedResponse,
)
from app.services.run_control_service import RunControlService  # 导入运行控制服务类型

# 获取模块级日志器。
# 直接使用标准库 logging，保持 interfaces 层不依赖 infra 包路径。
logger = logging.getLogger(__name__)


# 创建运行路由器
router = APIRouter()


def _build_get_run_response(run: Run) -> GetRunResponse:
    """把运行领域模型转换为查询运行响应。

    字段字符串化只属于 HTTP 表示层，不应下沉到业务层。
    """
    return GetRunResponse(
        run_id=run.run_id,  # Run ID
        session_id=run.session_id,  # 会话 ID
        status=run.status.value,  # 状态值
        created_at=run.created_at.isoformat(),  # 创建时间
        finished_at=run.finished_at.isoformat() if run.finished_at is not None else None,  # 结束时间
        output=run.output,  # 输出内容
        error_code=run.error_code,  # 错误码
        error_message=run.error_message,  # 错误消息
    )


@router.get(  # GET /runs/{run_id}
    "/runs/{run_id}",
    summary="查询运行详情",  # 接口摘要
)
async def get_run(
    run_id: str,  # 运行 ID
    run_control_service: RunControlService = Depends(get_run_control_service),  # 注入运行控制服务
):  # 查询运行
    """查询指定运行的详情。

    如果运行存在，返回包含 run_id、session_id、status 等字段的响应。
    如果运行不存在，返回 HTTP 200 + request_failed body。
    """
    logger.debug("查询运行请求: run_id=%s", run_id)

    # 直接通过注入的运行服务查询运行详情。
    # Route 只依赖运行查询能力本身，不再感知整个容器。
    run_result = await run_control_service.get_run(run_id)

    if isinstance(run_result, AppError):  # 运行不存在
        logger.error("运行不存在: run_id=%s", run_id)
        # 返回业务错误（HTTP 200 + request_failed body）
        return RequestFailedResponse(
            error_code=ErrorCode.RUN_NOT_FOUND,  # 错误码：运行不存在
            message=f"Run {run_id} not found",  # 错误消息
        )

    logger.debug("查询运行成功: run_id=%s, status=%s", run_id, run_result.status.value)
    # 在 HTTP 层完成响应序列化
    return _build_get_run_response(run_result)


@router.post(  # POST /runs/{run_id}/cancel
    "/runs/{run_id}/cancel",
    summary="取消运行",  # 接口摘要
)
async def cancel_run(
    run_id: str,  # 运行 ID
    run_control_service: RunControlService = Depends(get_run_control_service),  # 注入运行控制服务
):  # 取消运行
    """主动取消指定运行。

    仅当运行处于 RUNNING 状态时才允许发出取消信号；
    若运行不存在或已结束，则返回业务错误。
    """
    logger.info("取消运行请求: run_id=%s", run_id)

    run_result = await run_control_service.get_run(run_id)

    if isinstance(run_result, AppError):  # 运行不存在
        logger.error("取消运行失败，运行不存在: run_id=%s", run_id)
        return RequestFailedResponse(
            error_code=ErrorCode.RUN_NOT_FOUND,
            message=f"Run {run_id} not found",
        )

    if run_result.status != RunStatus.RUNNING:  # 运行已结束或非运行中状态
        logger.warning("取消运行失败，运行不在进行中: run_id=%s, status=%s", run_id, run_result.status.value)
        return RequestFailedResponse(
            error_code=ErrorCode.RUN_NOT_FOUND,  # 复用 RUN_NOT_FOUND 作为业务错误码
            message=f"Run {run_id} is not running",
        )

    cancelled = run_control_service.cancel_run(run_id)
    logger.info("取消运行成功: run_id=%s, cancelled=%s", run_id, cancelled)
    return CancelRunResponse(run_id=run_id, cancelled=cancelled)
