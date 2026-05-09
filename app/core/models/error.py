"""AppError 与 ErrorCode 定义。"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class ErrorCode(str, Enum):
    """应用级错误码枚举。

    所有服务层抛出的业务异常都会映射到该枚举中的某一值，
    以便于前端或调用方进行程序化错误处理。
    """

    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    RUN_NOT_FOUND = "RUN_NOT_FOUND"
    SESSION_RUN_CONFLICT = "SESSION_RUN_CONFLICT"
    SESSION_LOCK_HEARTBEAT_FAILED = "SESSION_LOCK_HEARTBEAT_FAILED"
    LLM_REQUEST_FAILED = "LLM_REQUEST_FAILED"
    HOOK_EXECUTION_FAILED = "HOOK_EXECUTION_FAILED"
    MAX_TURNS_EXCEEDED = "MAX_TURNS_EXCEEDED"
    CONTEXT_COMPRESSION_FAILED = "CONTEXT_COMPRESSION_FAILED"
    RUN_CANCELLED = "RUN_CANCELLED"
    UNKNOWN_SUBAGENT = "UNKNOWN_SUBAGENT"
    INVALID_SUBAGENT_CONFIG = "INVALID_SUBAGENT_CONFIG"
    CHILD_AGENT_EXECUTION_FAILED = "CHILD_AGENT_EXECUTION_FAILED"
    CHILD_AGENT_RECURSION_FORBIDDEN = "CHILD_AGENT_RECURSION_FORBIDDEN"
    CHILD_AGENT_CONTEXT_INVALID = "CHILD_AGENT_CONTEXT_INVALID"


class AppError(BaseModel):
    """表示一次应用层可暴露给外部的错误信息。

    该模型本身不是异常，而是错误的标准化数据结构，
    可用于事件、API 返回体或日志中。
    """

    error_code: ErrorCode
    message: str
