"""FastAPI 应用装配器。

当前模块只负责基于已准备好的配置与容器装配 FastAPI 壳层，
不再承担配置加载、日志初始化与容器构建职责。
"""

from __future__ import annotations  # 启用未来注解

import logging  # 导入标准库日志模块；入口层只从 infra 获取初始化能力
from fastapi import FastAPI  # 导入 FastAPI 框架
from fastapi.middleware.cors import CORSMiddleware  # 导入 CORS 中间件

from app.bootstrap.container import Container  # 导入依赖容器
from app.config import Settings  # 导入配置对象；纯装配器只消费已准备好的配置
from app.interfaces.http.exception_handlers import (  # 导入异常处理器
    general_exception_handler,
    http_exception_handler,
    validation_exception_handler,
)

# 获取模块级日志器。
# 当前模块只负责输出装配阶段日志，不负责初始化日志系统。
logger = logging.getLogger(__name__)


def create_app(settings: Settings, container: Container) -> FastAPI:
    """创建 FastAPI 应用实例。

    当前函数是纯装配器：只消费已经准备好的 Settings 与 Container，
    并负责把 Web 壳层相关的中间件、异常处理器、路由统一注册到应用上。

    Returns:
        配置完成的 FastAPI 应用实例
    """
    logger.debug("配置加载完成: app_env=%s, app_host=%s, app_port=%d", settings.app_env, settings.app_host, settings.app_port)

    # 创建 FastAPI 应用实例，配置 API 元数据
    is_dev = settings.is_dev  # 判断是否为开发环境
    app = FastAPI(
        title="Agent Framework API",  # API 标题
        description="AI Agent 框架 HTTP API 服务",  # API 描述
        version="0.1.0",  # API 版本号
        # 生产环境关闭 API 文档，开发环境保留
        docs_url="/docs" if is_dev else None,  # Swagger UI 路径
        redoc_url="/redoc" if is_dev else None,  # ReDoc 路径
    )  # 构造 FastAPI 应用

    # 注册 CORS 中间件
    app.add_middleware(
        CORSMiddleware,  # CORS 中间件类
        allow_origins=settings.cors_allow_origins,  # 允许的源地址
        allow_credentials=settings.cors_allow_credentials,  # 是否允许凭证
        allow_methods=settings.cors_allow_methods,  # 允许的 HTTP 方法
        allow_headers=settings.cors_allow_headers,  # 允许的请求头
    )  # 注册跨域中间件

    # 注册全局异常处理器
    from fastapi.exceptions import HTTPException  # 导入 HTTP 异常
    from fastapi.exceptions import RequestValidationError  # 导入验证异常

    # 注册 Pydantic 验证错误处理器
    app.add_exception_handler(
        RequestValidationError,  # 验证异常类型
        validation_exception_handler,  # 对应的处理器函数
    )  # 注册请求参数验证异常处理器
    logger.debug("注册异常处理器: RequestValidationError")

    # 注册 HTTP 异常处理器
    app.add_exception_handler(
        HTTPException,  # HTTP 异常类型
        http_exception_handler,  # 对应的处理器函数
    )  # 注册 HTTP 异常处理器

    # 注册兜底异常处理器，捕获所有未处理的异常
    app.add_exception_handler(
        Exception,  # 通用异常类型
        general_exception_handler,  # 兜底处理器函数
    )  # 注册通用异常处理器
    logger.debug("注册异常处理器完成")

    # 直接使用外部已构建好的容器并赋值到应用状态。
    # 不使用 lifespan，是为了兼容当前 `httpx.ASGITransport` 的测试路径。
    # 但仍需显式注册 startup/shutdown 事件，保证容器托管的 Redis / MCP 等资源能正确预热与释放。
    app.state.container = container  # 将容器存储到应用状态

    async def _on_startup() -> None:
        """应用启动时执行容器预热，提前就绪 Redis 与取消监听器。"""
        await container.startup()  # 统一委托给容器完成启动期预热，避免入口层感知更多内部资源细节

    app.router.add_event_handler("startup", _on_startup)  # 启动时预热连接池
    app.router.add_event_handler("shutdown", container.close)  # 在应用关闭时回收容器持有的基础设施资源
    logger.info("依赖容器已创建并注册到应用状态")

    from app.interfaces.http.routes.health import router as health_router  # 导入健康检查路由
    # 注册路由
    # 先注册健康检查路由（无依赖，最先注册）
    app.include_router(health_router)  # 注册健康检查路由
    logger.debug("注册路由: health")

    from app.interfaces.http.routes.sessions import router as sessions_router  # 导入会话路由
    from app.interfaces.http.routes.runs import router as runs_router  # 导入运行路由
    from app.interfaces.http.routes.chat import router as chat_router  # 导入聊天路由

    # 将业务路由注册到应用
    app.include_router(sessions_router)  # 注册会话路由
    app.include_router(runs_router)  # 注册运行路由
    app.include_router(chat_router)  # 注册聊天路由
    logger.info("路由注册完成: health, sessions, runs, chat")

    return app  # 返回配置完成的应用实例


# 当前模块只保留纯装配职责。
# 若要通过 uvicorn --factory 启动服务，应改用 `app.bootstrap.factory:bootstrap_app`。
