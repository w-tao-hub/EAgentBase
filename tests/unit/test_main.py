"""应用入口与装配器行为测试。"""

from __future__ import annotations

import logging
from inspect import isawaitable
from types import SimpleNamespace

import pytest

import app.bootstrap.factory as factory_module
from app.config import Settings
from app.infra.logging import shutdown_logging
from app.main import create_app


class StubContainer:
    """最小容器替身。"""

    def __init__(self) -> None:
        """初始化关闭标记。"""
        self.started = False  # 记录启动预热是否被触发，便于未来扩展 startup 生命周期断言
        self.closed = False  # 记录关闭事件是否真正触发容器关闭逻辑

    async def startup(self) -> None:
        """模拟容器启动预热方法。"""
        self.started = True  # 标记应用启动时已调用容器预热逻辑

    async def close(self) -> None:
        """模拟容器关闭方法。"""
        self.closed = True  # 标记应用关闭时已调用容器关闭逻辑


def test_bootstrap_app_runs_bootstrap_stages_in_order(monkeypatch) -> None:
    """验证公开启动入口只负责编排 bootstrap 阶段。"""
    calls: list[tuple[str, object]] = []
    settings = Settings(redis_url="redis://localhost:6379")  # 使用最小可用配置构造 Settings
    container = SimpleNamespace()  # 使用最小容器替身，避免依赖真实装配
    app = SimpleNamespace()  # 使用最小应用替身，聚焦编排顺序而非 FastAPI 细节

    def fake_load_settings():
        """替换配置加载阶段，返回预设配置对象。"""
        calls.append(("load_settings", None))
        return settings

    def fake_initialize_runtime(received_settings):
        """替换运行时初始化阶段，记录收到的配置对象。"""
        calls.append(("initialize_runtime", received_settings))

    def fake_build_container(received_settings):
        """替换容器构建阶段，记录收到的配置对象。"""
        calls.append(("build_container", received_settings))
        return container

    def fake_create_app(*, settings, container):
        """替换纯装配器，验证启动入口把依赖原样转交给装配层。"""
        calls.append(("create_app", (settings, container)))
        return app

    monkeypatch.setattr(factory_module, "load_settings", fake_load_settings)
    monkeypatch.setattr(factory_module, "initialize_runtime", fake_initialize_runtime)
    monkeypatch.setattr(factory_module, "build_container", fake_build_container)
    monkeypatch.setattr(factory_module, "create_app", fake_create_app)

    assert factory_module.bootstrap_app() is app
    assert calls == [
        ("load_settings", None),
        ("initialize_runtime", settings),
        ("build_container", settings),
        ("create_app", (settings, container)),
    ]


def test_create_app_only_assembles_fastapi_shell() -> None:
    """验证纯装配器只消费传入依赖并完成 FastAPI 壳层注册。"""
    shutdown_logging()  # 先清理日志状态，便于断言纯装配器不触碰日志全局初始化

    root_logger = logging.getLogger()  # 记录装配前的 handler 数量，验证无额外全局初始化动作
    handler_count_before = len(root_logger.handlers)

    settings = Settings(redis_url="redis://localhost:6379", app_env="dev")  # 显式指定 dev，避免本地环境变量覆盖文档开关断言
    container = StubContainer()  # 构造最小容器替身，并满足关闭事件注册所需的 close 接口

    app = create_app(settings=settings, container=container)

    assert app.state.container is container  # 验证纯装配器会把外部容器挂载到应用状态
    assert app.docs_url == "/docs"  # 开发环境默认保留 Swagger 文档
    assert app.redoc_url == "/redoc"  # 开发环境默认保留 ReDoc 文档
    assert {route.path for route in app.routes} >= {"/health/ready", "/sessions", "/sessions/{session_id}", "/runs/{run_id}", "/chat"}  # 验证关键路由已完成注册
    assert len(root_logger.handlers) == handler_count_before  # 验证纯装配器没有触发日志全局初始化


@pytest.mark.asyncio
async def test_create_app_registers_container_startup_on_startup() -> None:
    """验证应用启动时会调用容器启动预热逻辑。"""
    settings = Settings(redis_url="redis://localhost:6379")  # 构造最小配置对象
    container = StubContainer()  # 构造带启动标记的容器替身

    app = create_app(settings=settings, container=container)  # 创建应用实例，验证启动生命周期装配

    for startup_handler in app.router.on_startup:  # 遍历应用登记的启动处理器，模拟框架进入 startup 阶段
        result = startup_handler()  # 执行启动处理器，覆盖同步与异步两种处理器形态
        if isawaitable(result):  # 如果处理器返回 awaitable，说明它是异步启动逻辑
            await result  # 等待异步启动完成，确保断言前预热逻辑已执行

    assert container.started is True  # 验证启动事件确实触发了容器 startup 逻辑


@pytest.mark.asyncio
async def test_create_app_registers_container_close_on_shutdown() -> None:
    """验证应用关闭时会调用容器关闭逻辑。"""
    settings = Settings(redis_url="redis://localhost:6379")  # 构造最小配置对象
    container = StubContainer()  # 构造带关闭标记的容器替身

    app = create_app(settings=settings, container=container)  # 创建应用实例，验证关闭生命周期装配

    for shutdown_handler in app.router.on_shutdown:  # 遍历应用登记的关闭处理器，模拟框架进入 shutdown 阶段
        result = shutdown_handler()  # 执行关闭处理器，覆盖同步与异步两种处理器形态
        if isawaitable(result):  # 如果处理器返回 awaitable，说明它是异步关闭逻辑
            await result  # 等待异步关闭完成，确保断言前资源释放已执行

    assert container.closed is True  # 验证关闭事件确实触发了容器 close
