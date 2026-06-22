"""聊天路由单元测试。"""

from __future__ import annotations

import asyncio

import pytest

from app.core.models.event import RunStartedEvent
from app.interfaces.http.routes.chat import chat as chat_route
from app.interfaces.http.schemas.chat import ChatRequest


class StubRequest:
    """最小 Request 替身。

    只实现聊天路由实际会访问的 `receive()` 与 `is_disconnected()`，
    从而把测试关注点收敛到断连清理链，而不是 FastAPI/Starlette 细节。
    """

    def __init__(self) -> None:
        """初始化断连与等待控制事件。"""
        self._disconnect_event = asyncio.Event()  # 由测试显式触发，模拟客户端已断开

    async def receive(self) -> dict:
        """阻塞等待断连事件，再返回 http.disconnect。"""
        await self._disconnect_event.wait()  # 在测试触发前一直阻塞，模拟真实 monitor 正在等待 receive
        return {"type": "http.disconnect"}  # 断连发生后返回标准 ASGI disconnect 消息

    async def is_disconnected(self) -> bool:
        """返回当前是否已模拟断连。"""
        return self._disconnect_event.is_set()  # 直接复用事件状态，保持语义简单明确

    def mark_disconnected(self) -> None:
        """供测试手动标记断连。"""
        self._disconnect_event.set()  # 唤醒 monitor，使路由清理链进入断连处理路径


class StubChatService:
    """最小 ChatService 替身。

    通过一个带 finally 的异步生成器，验证路由层是否真的把上游生成器关闭到底。
    """

    def __init__(self) -> None:
        """初始化关闭完成事件。"""
        self.closed = asyncio.Event()  # 记录上游事件流的 finally 是否最终跑完
        self.cancel_event: asyncio.Event | None = None  # 记录路由透传下来的取消事件，便于断言

    async def stream_chat(self, session_id: str, master_agent_name: str, user_message: str, metadata: dict | None = None, cancel_event: asyncio.Event | None = None):
        """返回一个最小异步事件流，并在 finally 中模拟较慢清理。"""
        del session_id, master_agent_name, user_message, metadata  # 该替身只验证清理链，不消费业务参数
        self.cancel_event = cancel_event  # 保存路由注入的取消事件，便于测试验证断连信号已透传到服务层
        try:
            yield RunStartedEvent(run_id="run-route-test", session_id="session-route-test")  # 先产出一个事件，让外层流正式进入工作状态
            while True:
                await asyncio.sleep(10)  # 后续保持阻塞，确保测试主动关闭流时，上游仍处于活跃状态
        finally:
            await asyncio.sleep(0.05)  # 人为拉长 finally，稳定制造“清理过程中再次收到取消”的竞态窗口
            self.closed.set()  # 标记上游生成器已完成关闭，供测试断言 shield 清理生效


@pytest.mark.asyncio
async def test_chat_route_cleanup_survives_task_cancellation_during_disconnect() -> None:
    """测试断连清理协程即便再次收到取消，也会把上游事件流完整关闭。"""
    request = StubRequest()  # 构造最小 Request 替身，手动控制 disconnect 时机
    chat_service = StubChatService()  # 构造最小 ChatService 替身，观察上游生成器 finally 是否执行到底

    response = await chat_route(  # 直接调用聊天路由函数，拿到真实 StreamingResponse 与包装后的 body_iterator
        request=request,
        payload=ChatRequest(session_id="session-route-test", master_agent_name="default", message="hi"),
        chat_service=chat_service,
    )
    body_iterator = response.body_iterator  # 读取包装后的 SSE 生成器，后续直接操作 aclose 路径

    first_chunk = await anext(body_iterator)  # 先消费首个 chunk，确保上游聊天流与 monitor 任务都已正式启动
    assert "run_started" in first_chunk  # 验证当前确实已经进入流式响应阶段，避免测试落在未启动的假阳性路径

    request.mark_disconnected()  # 主动标记断连，触发 monitor 设置 cancel_event 并进入 finally 清理链

    close_task = asyncio.create_task(body_iterator.aclose())  # 在独立任务里触发流关闭，便于随后模拟“清理任务再次被取消”
    await asyncio.sleep(0.01)  # 给 finally 一点时间进入 shield 包裹的上游关闭逻辑
    close_task.cancel()  # 模拟真实断连场景里，请求任务本身在清理途中又被取消
    await close_task  # 修复后的行为应是：外层关闭任务会吞掉这次取消，并继续把上游清理完整做完

    await asyncio.wait_for(chat_service.closed.wait(), timeout=1)  # 核心断言：即使外层任务被取消，上游生成器 finally 也必须最终跑完
    assert chat_service.cancel_event is not None  # 断言路由确实向服务层透传了取消事件对象
    assert chat_service.cancel_event.is_set() is True  # 断言断连信号已经写入服务层 cancel_event，符合断连取消设计语义
