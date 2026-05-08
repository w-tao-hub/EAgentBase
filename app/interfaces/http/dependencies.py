"""HTTP 层依赖解析函数。

统一收口 Route 对应用状态中容器的读取逻辑。
Route 只声明自己需要的具体依赖，
不直接感知整个 Container 的内部结构。
"""

from __future__ import annotations  # 启用未来注解

from collections.abc import Awaitable, Callable  # 导入可调用与异步返回类型
from typing import TYPE_CHECKING  # 导入类型检查标记

from fastapi import Request  # 导入 FastAPI 请求对象

from app.services.chat_service import ChatService  # 导入聊天服务类型
from app.services.run_control_service import RunControlService  # 导入运行控制服务类型
from app.services.session_service import SessionService  # 导入会话服务类型

if TYPE_CHECKING:  # 仅在类型检查时导入，避免不必要的运行时耦合
    from app.bootstrap.container import Container  # 导入容器类型


def get_container(request: Request) -> "Container":
    """从应用状态中获取依赖容器。

    这里是 HTTP 层唯一允许感知 `app.state.container` 的位置。
    其他 Route 应通过更窄的依赖函数获取具体服务，避免直接接触整个容器。
    """
    return request.app.state.container  # 返回当前应用挂载的容器实例


def get_session_service(request: Request) -> SessionService:
    """获取会话服务。

    Route 通过该函数声明自己依赖会话能力，
    而不是直接操作整个容器。
    """
    return get_container(request).session_service  # 返回容器内装配好的会话服务


def get_run_control_service(request: Request) -> RunControlService:
    """获取运行控制服务。

    统一通过依赖函数暴露运行查询能力，
    避免 Route 直接访问容器成员。
    """
    return get_container(request).run_control_service  # 返回容器内装配好的运行服务


def get_chat_service(request: Request) -> ChatService:
    """获取聊天服务。

    聊天 Route 只需要聊天主链路能力，
    不需要知道容器内部还持有哪些其他依赖。
    """
    return get_container(request).chat_service  # 返回容器内装配好的聊天服务


def get_readiness_probe(request: Request) -> Callable[[], Awaitable[None]]:
    """获取 readiness 检查函数。

    健康检查 Route 只依赖“执行就绪探测”这一件事，
    不应再直接读取容器内部 `_redis` 等基础设施对象。
    """
    return get_container(request).ping_readiness  # 返回容器显式暴露的 readiness 探测入口
