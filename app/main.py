"""FastAPI 应用装配器。

只负责基于已准备好的配置与容器装配 FastAPI 壳层。
"""

from __future__ import annotations

import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.bootstrap.container import Container
from app.config import Settings
from app.interfaces.http.exception_handlers import (
    general_exception_handler,
    http_exception_handler,
    validation_exception_handler,
)

logger = logging.getLogger(__name__)


def create_app(settings: Settings, container: Container) -> FastAPI:
    """创建 FastAPI 应用实例。

    消费已准备好的 Settings 与 Container，
    注册中间件、异常处理器、路由。

    Returns:
        配置完成的 FastAPI 应用实例
    """
    logger.debug("配置加载完成: app_env=%s, app_host=%s, app_port=%d", settings.app_env, settings.app_host, settings.app_port)

    is_dev = settings.is_dev
    app = FastAPI(
        title="Agent Framework API",
        description="AI Agent 框架 HTTP API 服务",
        version="0.1.0",
        # 生产环境关闭 API 文档，开发环境保留
        docs_url="/docs" if is_dev else None,
        redoc_url="/redoc" if is_dev else None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=settings.cors_allow_methods,
        allow_headers=settings.cors_allow_headers,
    )

    from fastapi.exceptions import HTTPException
    from fastapi.exceptions import RequestValidationError

    app.add_exception_handler(
        RequestValidationError,
        validation_exception_handler,
    )
    logger.debug("注册异常处理器: RequestValidationError")

    app.add_exception_handler(
        HTTPException,
        http_exception_handler,
    )

    app.add_exception_handler(
        Exception,
        general_exception_handler,
    )
    logger.debug("注册异常处理器完成")

    # 不使用 lifespan，是为了兼容当前 httpx.ASGITransport 的测试路径。
    # 但仍需显式注册 startup/shutdown 事件，保证容器托管的 Redis / MCP 等资源能正确预热与释放。
    app.state.container = container

    async def _on_startup() -> None:
        """应用启动时执行容器预热。"""
        # 统一委托给容器完成启动期预热，避免入口层感知更多内部资源细节
        await container.startup()

    app.router.add_event_handler("startup", _on_startup)
    app.router.add_event_handler("shutdown", container.close)
    logger.info("依赖容器已创建并注册到应用状态")

    from app.interfaces.http.routes.health import router as health_router
    # 先注册健康检查路由（无依赖，最先注册）
    app.include_router(health_router)
    logger.debug("注册路由: health")

    from app.interfaces.http.routes.sessions import router as sessions_router
    from app.interfaces.http.routes.runs import router as runs_router
    from app.interfaces.http.routes.chat import router as chat_router

    app.include_router(sessions_router)
    app.include_router(runs_router)
    app.include_router(chat_router)
    logger.info("路由注册完成: health, sessions, runs, chat")

    return app
