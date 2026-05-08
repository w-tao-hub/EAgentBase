"""健康检查端点。

提供用于负载均衡器和容器编排平台（如 Kubernetes）的健康检查接口。
"""

from __future__ import annotations  # 启用未来注解

import logging  # 导入标准库日志模块，避免 interfaces 依赖 infra 包路径
from collections.abc import Awaitable, Callable  # 导入异步可调用类型

from fastapi import APIRouter  # 导入 FastAPI 路由组件
from fastapi import Depends  # 导入 FastAPI 依赖注入工具

from app.interfaces.http.dependencies import get_readiness_probe  # 导入 readiness 依赖函数

# 获取模块级日志器。
# 直接使用标准库 logging，保持 interfaces 层不依赖 infra 包路径。
logger = logging.getLogger(__name__)

# 创建健康检查路由实例
router = APIRouter(tags=["health"])  # 使用 health 标签归类接口


@router.get("/health")
async def health_check():
    """服务健康检查端点。

    返回服务基本健康状态，用于简单的可用性检查。

    Returns:
        包含状态和版本信息的字典：
        - status: "ok" 表示服务正常
        - version: API 版本号
    """
    return {
        "status": "ok",  # 服务状态
        "version": "0.1.0",  # API 版本号
    }


@router.get("/health/ready")
async def readiness_check(
    readiness_probe: Callable[[], Awaitable[None]] = Depends(get_readiness_probe),  # 注入 readiness 探测函数
):
    """就绪探针 - 检查依赖是否可用。

    用于 Kubernetes 等平台的就绪探针，检查所有依赖服务是否正常。
    如果依赖不可用，返回 503 状态码，让平台将流量路由到其他实例。

    Returns:
        200: 所有依赖正常
        503: 存在依赖不可用
    """
    try:
        # 通过显式注入的 readiness 能力执行依赖探测。
        # Route 不直接感知 Redis 客户端等基础设施细节。
        await readiness_probe()  # 执行 readiness 检查

        logger.debug("就绪检查通过: Redis 连接正常")
        return {
            "status": "ready",  # 就绪状态
            "checks": {
                "redis": "ok",  # Redis 连接正常
            },
        }
    except Exception as e:
        logger.error("就绪检查失败: Redis 连接异常, error=%s", e)
        # 依赖检查失败，返回 503 服务不可用
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=503,  # 503 服务不可用
            content={
                "status": "not_ready",  # 未就绪状态
                "checks": {
                    "redis": f"error: {str(e)}",  # Redis 错误信息
                },
            },
        )


@router.get("/health/live")
async def liveness_check():
    """存活探针 - 仅检查服务是否存活。

    用于 Kubernetes 等平台的存活探针，检查应用进程是否还在运行。
    如果返回非 200 状态码，平台会重启容器。

    Returns:
        简单的存活状态，只要应用进程存在就应返回 200
    """
    return {"status": "alive"}  # 应用存活状态
