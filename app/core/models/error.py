"""AppError 与 ErrorCode 定义。"""

from __future__ import annotations  # 启用未来注解，避免前向引用问题

from enum import Enum  # 导入枚举基类

from pydantic import BaseModel  # 导入 Pydantic v2 的基础模型


class ErrorCode(str, Enum):
    """应用级错误码枚举。

    所有服务层抛出的业务异常都会映射到该枚举中的某一值，
    以便于前端或调用方进行程序化错误处理。
    """

    # 目标会话不存在
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"

    # 目标运行记录不存在
    RUN_NOT_FOUND = "RUN_NOT_FOUND"

    # 会话下已有运行在进行中，无法并发启动新运行
    SESSION_RUN_CONFLICT = "SESSION_RUN_CONFLICT"

    # 会话锁心跳续期失败，当前运行失去锁 owner
    SESSION_LOCK_HEARTBEAT_FAILED = "SESSION_LOCK_HEARTBEAT_FAILED"

    # 调用大模型服务时发生请求失败
    LLM_REQUEST_FAILED = "LLM_REQUEST_FAILED"

    # Hook 执行过程中发生失败
    HOOK_EXECUTION_FAILED = "HOOK_EXECUTION_FAILED"

    # 运行轮数超过最大限制
    MAX_TURNS_EXCEEDED = "MAX_TURNS_EXCEEDED"

    # 上下文压缩过程中发生失败
    CONTEXT_COMPRESSION_FAILED = "CONTEXT_COMPRESSION_FAILED"

    # 运行被外部取消
    RUN_CANCELLED = "RUN_CANCELLED"

    # 指定的子代理类型不存在或未注册
    UNKNOWN_SUBAGENT = "UNKNOWN_SUBAGENT"

    # 子代理配置无效，缺少必需字段或格式不正确
    INVALID_SUBAGENT_CONFIG = "INVALID_SUBAGENT_CONFIG"

    # 子代理执行过程中发生失败
    CHILD_AGENT_EXECUTION_FAILED = "CHILD_AGENT_EXECUTION_FAILED"

    # 禁止子代理递归派发（子代理不能再次派发子代理）
    CHILD_AGENT_RECURSION_FORBIDDEN = "CHILD_AGENT_RECURSION_FORBIDDEN"

    # 子代理执行上下文无效或缺失关键信息
    CHILD_AGENT_CONTEXT_INVALID = "CHILD_AGENT_CONTEXT_INVALID"


class AppError(BaseModel):
    """表示一次应用层可暴露给外部的错误信息。

    该模型本身不是异常，而是错误的标准化数据结构，
    可用于事件、API 返回体或日志中。
    """

    # 错误码，使用 ErrorCode 枚举确保值域受控
    error_code: ErrorCode

    # 面向人类读者的错误描述文本
    message: str
