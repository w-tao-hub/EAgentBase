"""聊天路由。

处理 POST /chat 接口，返回 SSE 流式响应。
"""

from __future__ import annotations

import asyncio
import logging
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.interfaces.http.dependencies import get_chat_service
from app.interfaces.http.schemas.chat import ChatRequest
from app.interfaces.http.sse import encode_sse
from app.services.chat_service import ChatService

logger = logging.getLogger(__name__)


router = APIRouter()


@router.post(
    "/chat",
    summary="流式聊天",
)
async def chat(
    request: Request,
    payload: ChatRequest,
    chat_service: ChatService = Depends(get_chat_service),
):
    """执行流式聊天。

    接受 ChatRequest 请求体，返回 SSE 格式的事件流。
    所有业务错误通过 SSE request_failed 事件返回，而不是使用 HTTP 4xx/5xx 错误码。
    同时启动后台任务监控 SSE 连接状态，若客户端断开则触发运行取消。
    """
    logger.info("聊天请求: session_id=%s, master_agent_name=%s, message_length=%d", payload.session_id, payload.master_agent_name, len(payload.message))

    # 创建取消事件，用于 SSE 断开后通知 ChatService 中断运行。
    cancel_event = asyncio.Event()

    # 在 Starlette >= 1.0 (ASGI >= 2.4) 下，StreamingResponse 不再主动读取 receive，
    # 导致 request.is_disconnected() 的轮询检查无法及时感知客户端断开，因此改为阻塞式 receive。
    async def _disconnect_monitor() -> None:
        try:
            while True:
                message = await request.receive()
                if message.get("type") == "http.disconnect":
                    logger.error("DEBUG monitor detected disconnect, setting cancel_event")
                    cancel_event.set()
                    break
        except Exception as e:
            logger.error("Disconnect monitor error: %s", e)
            cancel_event.set()

    monitor_task = asyncio.create_task(_disconnect_monitor())

    event_iterator = chat_service.stream_chat(
        session_id=payload.session_id,
        master_agent_name=payload.master_agent_name,
        user_message=payload.message,
        metadata=payload.metadata,
        cancel_event=cancel_event,
    )

    sse_stream = encode_sse(event_iterator)

    logger.debug("聊天 SSE 流已创建: session_id=%s", payload.session_id)

    async def _close_upstream_streams() -> None:
        """关闭 SSE 编码层与上游事件流。

        断连时，当前请求任务本身也可能正处于被取消状态。
        若不使用 shield 保护，`event_iterator.aclose()` 可能在执行到一半时再次被 CancelledError 打断，
        导致 run 卡在 RUNNING 的竞态。
        """
        try:
            await sse_stream.aclose()
        finally:
            await event_iterator.aclose()

    # 包装 SSE 流，确保在流被完全消费或连接断开后才清理后台监控任务。
    # 若将取消放在路由函数的 finally 中，会在 return StreamingResponse 之前触发，
    # 导致监控任务在 SSE 实际传输前就被取消。
    async def _wrapped_sse_stream():
        try:
            async for chunk in sse_stream:
                yield chunk
        except Exception as e:
            # SSE 编码或上游事件流异常时，发出兜底错误事件，确保客户端能收到终态
            logger.error("SSE 流异常: error=%s", e, exc_info=True)
            try:
                fallback_data = json.dumps({
                    "type": "run_failed",
                    "error_code": "SSE_ENCODING_ERROR",
                    "session_id": payload.session_id,
                    "message": f"SSE encoding error: {str(e)}",
                }, ensure_ascii=False)
                yield f"event: run_failed\ndata: {fallback_data}\n\n"
            except Exception:
                pass
        finally:
            disconnected = await request.is_disconnected()
            if disconnected:
                cancel_event.set()
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass
            # 使用独立清理任务 + shield，确保断连时上游生成器的 finally 仍能完整执行
            cleanup_task = asyncio.create_task(_close_upstream_streams())
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                await asyncio.shield(cleanup_task)

    return StreamingResponse(
        _wrapped_sse_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Content-Type": "text/event-stream; charset=utf-8",
        },
    )
