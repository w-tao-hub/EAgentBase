"""HTTP 层依赖解析函数。

统一收口 Route 对应用状态中容器的读取逻辑。
Route 只声明自己需要的具体依赖，
不直接感知整个 Container 的内部结构。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from fastapi import Request

from app.services.chat_service import ChatService
from app.services.run_control_service import RunControlService
from app.services.session_service import SessionService

if TYPE_CHECKING:
    from app.bootstrap.container import Container


def get_container(request: Request) -> "Container":
    """从应用状态中获取依赖容器。

    这里是 HTTP 层唯一允许感知 `app.state.container` 的位置。
    其他 Route 应通过更窄的依赖函数获取具体服务，避免直接接触整个容器。
    """
    return request.app.state.container


def get_session_service(request: Request) -> SessionService:
    """获取会话服务。"""
    return get_container(request).session_service


def get_run_control_service(request: Request) -> RunControlService:
    """获取运行控制服务。"""
    return get_container(request).run_control_service


def get_chat_service(request: Request) -> ChatService:
    """获取聊天服务。"""
    return get_container(request).chat_service


def get_readiness_probe(request: Request) -> Callable[[], Awaitable[None]]:
    """获取 readiness 检查函数。"""
    return get_container(request).ping_readiness
