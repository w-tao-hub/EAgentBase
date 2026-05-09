"""健康检查端点。

提供用于负载均衡器和容器编排平台（如 Kubernetes）的健康检查接口。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from fastapi import APIRouter
from fastapi import Depends

from app.interfaces.http.dependencies import get_readiness_probe

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check():
    """服务健康检查端点。"""
    return {
        "status": "ok",
        "version": "0.1.0",
    }


@router.get("/health/ready")
async def readiness_check(
    readiness_probe: Callable[[], Awaitable[None]] = Depends(get_readiness_probe),
):
    """就绪探针 - 检查依赖是否可用。"""
    try:
        await readiness_probe()

        logger.debug("就绪检查通过: Redis 连接正常")
        return {
            "status": "ready",
            "checks": {
                "redis": "ok",
            },
        }
    except Exception as e:
        logger.error("就绪检查失败: Redis 连接异常, error=%s", e)
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "checks": {
                    "redis": f"error: {str(e)}",
                },
            },
        )


@router.get("/health/live")
async def liveness_check():
    """存活探针 - 仅检查服务是否存活。"""
    return {"status": "alive"}
