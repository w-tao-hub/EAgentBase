"""全局 HTTP 异常处理器。

集中处理应用中各类异常，返回统一格式的错误响应。
"""

from __future__ import annotations

import logging
from fastapi import Request
from fastapi.exceptions import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """处理 Pydantic 请求参数验证错误。

    当请求体、查询参数或路径参数不符合 schema 定义时触发。
    将 Pydantic 的详细错误信息转换为友好的中文错误消息。

    Args:
        request: FastAPI 请求对象
        exc: Pydantic 验证异常实例

    Returns:
        422 状态码的 JSON 响应，仅包含错误摘要
    """
    errors = exc.errors()
    first_error = errors[0] if errors else {}
    location = " -> ".join(str(loc) for loc in first_error.get("loc", []))
    message = first_error.get("msg", "请求参数验证失败")

    logger.error("请求参数验证失败: %s - %s", location, message)

    return JSONResponse(
        status_code=422,
        content={
            "error": "VALIDATION_ERROR",
            "message": f"参数验证失败: {location} - {message}",
        },
    )


async def http_exception_handler(
    request: Request,
    exc: HTTPException,
) -> JSONResponse:
    """处理标准 HTTP 异常。

    处理 FastAPI 的 HTTPException，保持默认行为但统一响应格式。

    Args:
        request: FastAPI 请求对象
        exc: HTTP 异常实例

    Returns:
        对应状态码的 JSON 响应
    """
    message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)

    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": "HTTP_ERROR",
            "message": message,
        },
    )


async def general_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """处理未捕获的通用异常。

    作为最后的兜底处理器，捕获所有未被其他处理器处理的异常。
    生产环境中应避免暴露过多内部信息。

    Args:
        request: FastAPI 请求对象
        exc: 异常实例

    Returns:
        500 状态码的 JSON 响应
    """
    logger.error("未捕获的异常: %s", exc, exc_info=True)

    return JSONResponse(
        status_code=500,
        content={
            "error": "INTERNAL_ERROR",
            "message": "服务器内部错误，请稍后重试",
        },
    )
