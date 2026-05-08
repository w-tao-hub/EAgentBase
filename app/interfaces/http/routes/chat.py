"""聊天路由。

处理 POST /chat 接口，返回 SSE 流式响应。
"""

from __future__ import annotations  # 启用未来注解

import asyncio  # 导入异步模块，用于创建取消事件和后台监控任务
import logging  # 导入标准库日志模块，避免 interfaces 依赖 infra 包路径
from fastapi import APIRouter, Depends, Request  # 导入 FastAPI 路由、依赖注入和请求对象
from fastapi.responses import StreamingResponse  # 导入流式响应类

from app.interfaces.http.dependencies import get_chat_service  # 导入聊天服务依赖函数
from app.interfaces.http.schemas.chat import ChatRequest  # 导入聊天请求 Schema
from app.interfaces.http.sse import encode_sse  # 导入 SSE 编码工具
from app.services.chat_service import ChatService  # 导入聊天服务类型

# 获取模块级日志器。
# 直接使用标准库 logging，保持 interfaces 层不依赖 infra 包路径。
logger = logging.getLogger(__name__)


# 创建聊天路由器
router = APIRouter()


@router.post(  # POST /chat
    "/chat",
    summary="流式聊天",  # 接口摘要
)
async def chat(
    request: Request,  # FastAPI 请求对象，用于检测 SSE 客户端断开
    payload: ChatRequest,  # 聊天请求体
    chat_service: ChatService = Depends(get_chat_service),  # 注入聊天服务
):  # 聊天接口
    """执行流式聊天。

    接受 ChatRequest 请求体，返回 SSE 格式的事件流。
    所有业务错误（如会话不存在、会话冲突）都通过 SSE request_failed 事件返回，
    而不是使用 HTTP 4xx/5xx 错误码。
    同时启动后台任务监控 SSE 连接状态，若客户端断开则触发运行取消。

    Args:
        request: FastAPI 请求对象
        payload: 聊天请求体，由 FastAPI 自动解析和校验
    """
    logger.info("聊天请求: session_id=%s, message_length=%d", payload.session_id, len(payload.message))

    # 创建取消事件，用于 SSE 断开后通知 ChatService 中断运行。
    cancel_event = asyncio.Event()

    # 启动后台监控任务，阻塞等待 ASGI http.disconnect 消息。
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

    # 将取消事件传入聊天服务，实现运行中断。
    event_iterator = chat_service.stream_chat(
        session_id=payload.session_id,  # 会话 ID
        user_message=payload.message,  # 用户消息
        metadata=payload.metadata,  # 元数据
        cancel_event=cancel_event,  # 取消事件
    )

    # 将事件流编码为 SSE 格式
    sse_stream = encode_sse(event_iterator)  # 编码为 SSE 文本流

    logger.debug("聊天 SSE 流已创建: session_id=%s", payload.session_id)

    async def _close_upstream_streams() -> None:
        """关闭 SSE 编码层与上游事件流。

        断连时，当前请求任务本身也可能正处于被取消状态。
        若不把关闭过程做成独立清理协程并在外层使用 shield 保护，
        `event_iterator.aclose()` 可能在执行到一半时再次被 CancelledError 打断，
        从而让 ChatService 的 finally 无法完整跑完，出现 run 卡在 RUNNING 的竞态。
        """
        try:
            await sse_stream.aclose()  # 先关闭 SSE 编码层，停止后续 chunk 编码
        finally:
            await event_iterator.aclose()  # 无论 SSE 编码层是否正常关闭，都必须继续关闭上游聊天生成器

    # 包装 SSE 流，确保在流被完全消费或连接断开后才清理后台监控任务。
    # 若将 cancel 放在路由函数的 finally 中，会在 return StreamingResponse 之前触发，
    # 导致监控任务在 SSE 实际传输前就被取消，客户端断开无法被检测。
    async def _wrapped_sse_stream():
        try:
            async for chunk in sse_stream:
                yield chunk
        except Exception as e:
            # SSE 编码或上游事件流异常时，发出兜底错误事件，
            # 确保客户端能收到终态，连接不会无声中断。
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
            # 在取消 monitor 前显式再检查一次断开，避免断开恰好发生在 monitor 的 sleep 窗口内。
            disconnected = await request.is_disconnected()
            if disconnected:
                cancel_event.set()
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass
            # 显式关闭 SSE 编码层，并进一步关闭上游的 chat_service.stream_chat。
            # 这里使用独立清理任务 + shield，确保断连时即便当前请求任务继续收到取消，
            # 上游生成器的 finally 仍能完整执行到结束，避免 run 卡在 RUNNING。
            cleanup_task = asyncio.create_task(_close_upstream_streams())
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                await asyncio.shield(cleanup_task)

    # 返回 SSE 流式响应
    return StreamingResponse(
        _wrapped_sse_stream(),  # 包装后的 SSE 文本流
        media_type="text/event-stream",  # SSE MIME 类型
        headers={
            # 禁止缓存，确保客户端实时接收事件
            "Cache-Control": "no-cache",
            # 保持连接存活
            "Connection": "keep-alive",
            # 禁用 Nginx/代理服务器缓冲，确保真正的实时流式传输
            # "X-Accel-Buffering": "no",
            # 明确指定字符编码
            "Content-Type": "text/event-stream; charset=utf-8",
        },
    )
