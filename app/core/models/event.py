"""事件模型定义。"""

from __future__ import annotations  # 启用未来注解，避免前向引用问题

from abc import ABC, abstractmethod  # 导入抽象基类和抽象方法装饰器
from typing import Optional  # 导入可选类型

from pydantic import BaseModel  # 导入 Pydantic v2 的基础模型

from app.core.models.error import ErrorCode  # 导入错误码枚举，确保事件也使用受控错误词汇
from app.core.models.stored_message import StoredMessage  # 导入消息模型，承载内部附带消息。


class Event(ABC, BaseModel):
    """所有业务事件的抽象基类。

    事件模型统一继承该基类，确保每个事件都有 event_name 属性和
    to_payload 方法，方便 SSE、WebSocket 或消息队列进行序列化。
    """

    @property
    @abstractmethod
    def event_name(self) -> str:
        """返回事件的字符串标识名，用于协议分发。"""
        ...

    @abstractmethod
    def to_payload(self) -> dict:
        """将事件序列化为字典形式的 payload，用于网络传输或存储。"""
        ...


class ExternalEvent(Event):
    """允许向客户端或外部调用方转发的公开协议事件。"""


class InternalEvent(Event):
    """仅供服务层或编排层内部消费的内部事件。"""


class RunStartedEvent(ExternalEvent):
    """当一次 Run 被成功创建并开始执行时发出的事件。"""

    # 启动的运行唯一标识
    run_id: str

    # 运行所属的会话唯一标识
    session_id: str

    @property
    def event_name(self) -> str:
        """返回事件名称 run_started。"""
        return "run_started"

    def to_payload(self) -> dict:
        """序列化为包含 type、run_id、session_id 的字典。"""
        return {
            "type": self.event_name,  # type 字段与 event_name 保持一致
            "run_id": self.run_id,
            "session_id": self.session_id,
        }


class MessageDeltaEvent(ExternalEvent):
    """当 Run 执行过程中大模型返回流式内容片段时发出的事件。"""

    # 该增量内容所属的运行标识
    run_id: str

    # 新增的内容片段，可能是一个 token 或一段文本
    content: str

    @property
    def event_name(self) -> str:
        """返回事件名称 message_delta。"""
        return "message_delta"

    def to_payload(self) -> dict:
        """序列化为包含 type、run_id、content 的字典。"""
        return {
            "type": self.event_name,
            "run_id": self.run_id,
            "content": self.content,
        }


class RunCompletedEvent(ExternalEvent):
    """当一次 Run 正常完成并产出最终结果时发出的事件。"""

    # 完成的运行唯一标识
    run_id: str

    # 运行最终输出的完整内容
    output: str

    # 本轮 assistant 的思考内容，仅供服务层持久化与后续上下文回放使用。
    reasoning_content: Optional[str] = None

    @property
    def event_name(self) -> str:
        """返回事件名称 run_completed。"""
        return "run_completed"

    def to_payload(self) -> dict:
        """序列化为包含 type、run_id、output 的字典。"""
        return {
            "type": self.event_name,
            "run_id": self.run_id,
            "output": self.output,
        }


class RunFailedEvent(ExternalEvent):
    """当一次 Run 执行过程中因内部错误中断时发出的事件。"""

    # 失败的运行唯一标识
    run_id: str

    # 导致失败的错误码，使用 ErrorCode 枚举进行强类型约束
    error_code: ErrorCode

    # 错误的人类可读描述
    message: str

    @property
    def event_name(self) -> str:
        """返回事件名称 run_failed。"""
        return "run_failed"

    def to_payload(self) -> dict:
        """序列化为包含 type、run_id、error_code、message 的字典。"""
        return {
            "type": self.event_name,
            "run_id": self.run_id,
            "error_code": self.error_code,
            "message": self.message,
        }


class RunCancelledEvent(ExternalEvent):
    """当一次 Run 被外部主动取消时发出的事件。"""

    # 被取消的运行唯一标识
    run_id: str

    # 取消原因的人类可读描述
    reason: str

    # 取消对应的错误码，使用 ErrorCode 枚举进行强类型约束
    error_code: ErrorCode

    @property
    def event_name(self) -> str:
        """返回事件名称 run_cancelled。"""
        return "run_cancelled"

    def to_payload(self) -> dict:
        """序列化为包含 type、run_id、reason、error_code 的字典。"""
        return {
            "type": self.event_name,
            "run_id": self.run_id,
            "reason": self.reason,
            "error_code": self.error_code,
        }


class RequestFailedEvent(ExternalEvent):
    """当外部请求（如启动 Run）在入参校验或业务校验阶段失败时发出的事件。

    与 RunFailedEvent 的区别在于：RunFailedEvent 针对已进入执行流程的 Run，
    而 RequestFailedEvent 针对尚未产生 Run 或运行已直接因请求问题被拒绝的场景。
    """

    # 请求失败的错误码，使用 ErrorCode 枚举进行强类型约束
    error_code: ErrorCode

    # 错误的人类可读描述
    message: str

    # 可选的关联运行标识；如果请求阶段尚未创建 Run，则可为 None
    run_id: Optional[str] = None

    @property
    def event_name(self) -> str:
        """返回事件名称 request_failed。"""
        return "request_failed"

    def to_payload(self) -> dict:
        """序列化为包含 type、error_code、message、run_id 的字典。

        无论 run_id 是否为 None，均将其包含在 payload 中，保证下游消费者
        面对的 schema 始终是稳定的，无需处理"键缺失"的情况。
        """
        return {
            "type": self.event_name,
            "error_code": self.error_code,
            "message": self.message,
            "run_id": self.run_id,
        }


class AssistantWithToolsEvent(InternalEvent):
    """需要持久化的assistant消息（包含tool_calls）。

    当AgentLoop检测到LLM返回tool_calls时发出此事件，
    用于ChatService存储带工具调用的assistant消息。

    该事件属于内部编排事件，只允许在服务层内部消费，
    不应被直接透传给客户端。

    注意：此事件在ToolUseStartedEvent之前发出，
    携带的是完整的assistant消息信息（content + 所有tool_calls）。
    """

    # 所属运行的唯一标识
    run_id: str

    # assistant的思考文本(content)，可能为空（当LLM只返回tool_calls时）
    content: Optional[str] = None

    # assistant 的 reasoning_content，只供内部持久化与后续回放使用。
    reasoning_content: Optional[str] = None

    # 完整的tool_calls列表（OpenAI格式）
    tool_calls: list

    @property
    def event_name(self) -> str:
        """返回事件名称 assistant_with_tools。"""
        return "assistant_with_tools"

    def to_payload(self) -> dict:
        """序列化为包含 type、run_id、content、tool_calls 的字典。"""
        return {
            "type": self.event_name,
            "run_id": self.run_id,
            "content": self.content,
            "tool_calls": self.tool_calls,
        }


class ToolUseStartedEvent(ExternalEvent):
    """当工具调用开始时发出的事件。

    在智能体决定调用某个工具时触发，通知下游消费者工具执行已开始。
    """

    # 所属运行的唯一标识
    run_id: str

    # 被调用工具的名称
    tool_name: str

    # 工具调用的唯一标识，用于关联开始和完成事件
    tool_call_id: str

    # 工具输入参数，必须符合工具的 input_schema
    tool_input: dict

    # 可选的额外内容，可用于传递上下文信息
    content: Optional[str] = None

    @property
    def event_name(self) -> str:
        """返回事件名称 tool_use_started。"""
        return "tool_use_started"

    def to_payload(self) -> dict:
        """序列化为包含 type、run_id、tool_name、tool_call_id、tool_input、content 的字典。"""
        return {
            "type": self.event_name,
            "run_id": self.run_id,
            "tool_name": self.tool_name,
            "tool_call_id": self.tool_call_id,
            "tool_input": self.tool_input,
            "content": self.content,
        }


class ToolUseCompletedEvent(ExternalEvent):
    """当工具调用完成时发出的事件。

    在工具执行完毕并返回结果后触发，通知下游消费者工具执行已结束。
    """

    # 所属运行的唯一标识
    run_id: str

    # 被调用工具的名称
    tool_name: str

    # 工具调用的唯一标识，用于关联开始和完成事件
    tool_call_id: str

    # 标记工具执行是否出错
    is_error: bool

    # 工具执行结果的内容
    result: str

    # 工具可选附带的内部存储消息，仅供服务层与循环编排层消费。
    stored_message: StoredMessage | None = None

    # Task 等工具的内部结构化元数据，仅供服务层消费。
    task_child_id: str | None = None

    @property
    def event_name(self) -> str:
        """返回事件名称 tool_use_completed。"""
        return "tool_use_completed"

    def to_payload(self) -> dict:
        """序列化为包含 type、run_id、tool_name、tool_call_id、is_error、result 的字典。"""
        return {
            "type": self.event_name,
            "run_id": self.run_id,
            "tool_name": self.tool_name,
            "tool_call_id": self.tool_call_id,
            "is_error": self.is_error,
            "result": self.result,
        }
