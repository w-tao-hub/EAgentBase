"""领域模型、错误码与事件模型的单元测试。"""

from __future__ import annotations  # 启用未来注解，避免前向引用问题

from datetime import datetime, timezone  # 导入日期时间类和时区类

import pytest  # 导入 pytest，用于断言非法状态组合会触发校验异常
from pydantic import ValidationError  # 导入 Pydantic 校验异常类型

from app.core.models.agent import Agent  # 导入 Agent 领域模型
from app.core.models.error import AppError, ErrorCode  # 导入错误模型和错误码枚举
from app.core.models.event import (  # 导入各类事件模型
    AssistantWithToolsEvent,
    Event,
    ExternalEvent,
    InternalEvent,
    MessageDeltaEvent,
    RequestFailedEvent,
    RunCompletedEvent,
    RunFailedEvent,
    RunStartedEvent,
    ToolUseCompletedEvent,
    ToolUseStartedEvent,
)
from app.core.models.run import ExecutionMode, Run, RunStatus, RunType  # 导入运行记录模型和状态枚举
from app.core.models.session import Session  # 导入会话领域模型
from app.core.models.stored_message import StoredMessage, StoredMessageMeta  # 导入直接存储消息模型


# ---------------------------------------------------------------------------
# StoredMessage 序列化与反序列化测试
# ---------------------------------------------------------------------------

def test_stored_message_create_sets_meta_fields() -> None:
    """验证 StoredMessage.create 会正确设置核心 `_meta` 字段。"""
    message = StoredMessage.create(
        role="user",
        content="hi",
        timestamp=datetime(2026, 4, 3, tzinfo=timezone.utc),
        message_id="abc123",
        is_meta=True,
        source_run_id="run-1",
    )

    assert message.role == "user"
    assert message.content == "hi"
    assert message.message_id == "abc123"
    assert message.is_meta is True
    assert message.timestamp == datetime(2026, 4, 3, tzinfo=timezone.utc)
    assert message.meta.source_run_id == "run-1"


def test_stored_message_round_trips_with_model_protocol_fields() -> None:
    """验证 StoredMessage 能按模型协议字段直接序列化和反序列化。"""
    message = StoredMessage(
        role="assistant",  # 角色为 assistant
        content="我先调用一个 child",  # 内容为普通文本
        reasoning_content="先规划一下调用步骤",  # reasoning_content 会被后续 user 轮次原样回传给模型
        tool_calls=[  # 附带一条工具调用协议数据
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "dispatch_child_agent",
                    "arguments": "{\"child_id\":\"writer-1\"}",
                },
            }
        ],
        meta=StoredMessageMeta(
            created_at=datetime(2026, 4, 30, 9, 0, 0, tzinfo=timezone.utc),
            source_run_id="run-master-1",
            child_id="writer-1",
        ),
    )

    data = message.to_storage_dict()
    restored = StoredMessage.from_storage_dict(data)

    assert data["_meta"]["created_at"] == "2026-04-30T09:00:00+00:00"
    assert data["_meta"]["source_run_id"] == "run-master-1"
    assert data["_meta"]["child_id"] == "writer-1"
    assert restored.meta.source_run_id == "run-master-1"
    assert restored.meta.child_id == "writer-1"
    assert restored.reasoning_content == "先规划一下调用步骤"
    assert restored.tool_calls is not None
    assert restored.tool_calls[0]["function"]["name"] == "dispatch_child_agent"
    assert restored.message_id == message.message_id
    assert restored.timestamp == datetime(2026, 4, 30, 9, 0, 0, tzinfo=timezone.utc)


def test_stored_message_meta_defaults_message_id_and_is_meta() -> None:
    """验证 StoredMessageMeta 会自动生成 message_id 并保留默认 is_meta。"""
    meta = StoredMessageMeta(
        created_at=datetime(2026, 4, 30, 10, 0, 0, tzinfo=timezone.utc),
    )

    data = meta.to_storage_dict()

    assert isinstance(meta.message_id, str)
    assert meta.message_id
    assert data["is_meta"] is False


# ---------------------------------------------------------------------------
# Agent 基本构造与序列化测试
# ---------------------------------------------------------------------------

def test_agent_basic_construction() -> None:
    """验证 Agent 模型的基本构造和字段正确性。"""
    # 构造一个标准 Agent 实例
    agent = Agent(
        agent_id="agent-1",  # 代理唯一标识
        name="Test Agent",  # 代理显示名称
        model="gpt-4.1-mini",  # 使用的大模型名称
        system_prompt="You are a test agent.",  # 系统提示词
        temperature=0.5,  # 采样温度
    )
    # 验证各字段与构造入参一致
    assert agent.agent_id == "agent-1"
    assert agent.name == "Test Agent"
    assert agent.model == "gpt-4.1-mini"
    assert agent.system_prompt == "You are a test agent."
    assert agent.temperature == 0.5


def test_agent_serializes_to_dict() -> None:
    """验证 Agent 模型可以通过 model_dump 序列化为字典。"""
    agent = Agent(
        agent_id="agent-1",
        name="Test Agent",
        model="gpt-4.1-mini",
        system_prompt="You are a test agent.",
        temperature=0.5,
    )
    # 使用 Pydantic 提供的 model_dump 方法进行序列化
    data = agent.model_dump()
    # 断言字典中包含预期的关键字段
    assert data["agent_id"] == "agent-1"
    assert data["name"] == "Test Agent"


# ---------------------------------------------------------------------------
# Session 基本构造与序列化测试
# ---------------------------------------------------------------------------

def test_session_basic_construction() -> None:
    """验证 Session 模型的基本构造和字段正确性。"""
    # 构造一个标准 Session 实例
    now = datetime(2026, 4, 3, 12, 0, 0, tzinfo=timezone.utc)  # 当前时间戳
    session = Session(
        session_id="sess-1",  # 会话唯一标识
        agent_id="agent-1",  # 所属代理标识
        created_at=now,  # 会话创建时间
    )
    assert session.session_id == "sess-1"
    assert session.agent_id == "agent-1"
    assert session.created_at == now


def test_session_serializes_to_dict() -> None:
    """验证 Session 模型可以通过 model_dump 序列化为字典。"""
    now = datetime(2026, 4, 3, 12, 0, 0, tzinfo=timezone.utc)
    session = Session(
        session_id="sess-1",
        agent_id="agent-1",
        created_at=now,
    )
    data = session.model_dump()
    # 断言序列化结果包含预期字段
    assert data["session_id"] == "sess-1"
    assert data["agent_id"] == "agent-1"


# ---------------------------------------------------------------------------
# Run 基本构造与序列化测试
# ---------------------------------------------------------------------------

def test_run_basic_construction() -> None:
    """验证 Run 模型的基本构造和字段正确性。"""
    now = datetime(2026, 4, 3, 12, 0, 0, tzinfo=timezone.utc)
    run = Run(
        run_id="run-1",  # 运行唯一标识
        session_id="sess-1",  # 关联会话标识
        status=RunStatus.RUNNING,  # 运行状态为运行中
        created_at=now,  # 创建时间
        finished_at=None,  # 尚未结束，所以为 None
        output=None,  # 尚无输出
        error_code=None,  # 尚无错误码
        error_message=None,  # 尚无错误信息
    )
    assert run.run_id == "run-1"
    assert run.session_id == "sess-1"
    assert run.status == RunStatus.RUNNING
    assert run.run_type == RunType.MASTER
    assert run.execution_mode == ExecutionMode.FOREGROUND
    assert run.created_at == now
    assert run.updated_at == now
    assert run.finished_at is None
    assert run.output is None
    assert run.error_code is None
    assert run.error_message is None


def test_run_serializes_to_dict() -> None:
    """验证 Run 模型可以通过 model_dump 序列化为字典。"""
    now = datetime(2026, 4, 3, 12, 0, 0, tzinfo=timezone.utc)
    run = Run(
        run_id="run-1",
        session_id="sess-1",
        status=RunStatus.COMPLETED,
        agent_id="master-agent",
        created_at=now,
        updated_at=now,
        finished_at=now,
        output="done",  # 运行输出内容
        error_code=None,
        error_message=None,
    )
    data = run.model_dump()
    assert data["run_id"] == "run-1"
    assert data["status"] == "completed"  # RunStatus 会序列化为其字符串值
    assert data["agent_id"] == "master-agent"
    assert data["output"] == "done"


def test_child_run_requires_parent_child_and_tool_call_id() -> None:
    """验证 child run 必须带齐单层派发所需的关系字段。"""
    now = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)

    with pytest.raises(ValidationError):
        Run(
            run_id="run-child-1",
            session_id="sess-1",
            run_type=RunType.CHILD,
            status=RunStatus.RUNNING,
            created_at=now,
            parent_run_id="run-master-1",
            child_id="writer-1",
            tool_call_id=None,
        )


def test_master_run_rejects_child_only_fields() -> None:
    """验证 master run 不能误带 child 专属关系字段。"""
    now = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)

    with pytest.raises(ValidationError):
        Run(
            run_id="run-master-1",
            session_id="sess-1",
            run_type=RunType.MASTER,
            status=RunStatus.RUNNING,
            created_at=now,
            child_id="writer-1",
        )


def test_running_run_rejects_terminal_fields() -> None:
    """验证 running 状态不能携带 finished_at、output 或错误字段。"""
    # 准备一个合法的创建时间，作为运行开始时间
    now = datetime(2026, 4, 3, 12, 0, 0, tzinfo=timezone.utc)
    # 断言当 running 状态携带终态字段时，会触发模型校验失败
    with pytest.raises(ValidationError):
        Run(
            run_id="run-1",  # 运行唯一标识
            session_id="sess-1",  # 所属会话标识
            status=RunStatus.RUNNING,  # 当前状态为运行中
            created_at=now,  # 创建时间
            finished_at=now,  # running 不允许已有结束时间
            output="done",  # running 不允许已有最终输出
            error_code=None,  # 本例不设置错误码
            error_message=None,  # 本例不设置错误信息
        )


def test_completed_run_requires_finished_at_and_output() -> None:
    """验证 completed 状态必须同时具备 finished_at 和 output。"""
    # 准备一个合法的创建时间，作为运行开始时间
    now = datetime(2026, 4, 3, 12, 0, 0, tzinfo=timezone.utc)
    # 断言缺少 finished_at 时，completed 状态会被拒绝
    with pytest.raises(ValidationError):
        Run(
            run_id="run-1",  # 运行唯一标识
            session_id="sess-1",  # 所属会话标识
            status=RunStatus.COMPLETED,  # 当前状态为已完成
            created_at=now,  # 创建时间
            finished_at=None,  # 已完成状态必须有结束时间
            output="done",  # 已完成状态已有最终输出
            error_code=None,  # 已完成状态不应携带错误码
            error_message=None,  # 已完成状态不应携带错误信息
        )
    # 断言缺少 output 时，completed 状态同样会被拒绝
    with pytest.raises(ValidationError):
        Run(
            run_id="run-2",  # 运行唯一标识
            session_id="sess-1",  # 所属会话标识
            status=RunStatus.COMPLETED,  # 当前状态为已完成
            created_at=now,  # 创建时间
            finished_at=now,  # 已完成状态有结束时间
            output=None,  # 已完成状态必须有最终输出
            error_code=None,  # 已完成状态不应携带错误码
            error_message=None,  # 已完成状态不应携带错误信息
        )


def test_failed_run_requires_finished_at_and_error_fields() -> None:
    """验证 failed 状态必须具备 finished_at、error_code 和 error_message。"""
    # 准备一个合法的创建时间，作为运行开始时间
    now = datetime(2026, 4, 3, 12, 0, 0, tzinfo=timezone.utc)
    # 断言缺少错误码时，failed 状态会被拒绝
    with pytest.raises(ValidationError):
        Run(
            run_id="run-1",  # 运行唯一标识
            session_id="sess-1",  # 所属会话标识
            status=RunStatus.FAILED,  # 当前状态为失败
            created_at=now,  # 创建时间
            finished_at=now,  # 失败状态已有结束时间
            output=None,  # 失败状态不应携带最终输出
            error_code=None,  # 失败状态必须有错误码
            error_message="boom",  # 失败状态已有错误描述
        )
    # 断言缺少错误描述时，failed 状态同样会被拒绝
    with pytest.raises(ValidationError):
        Run(
            run_id="run-2",  # 运行唯一标识
            session_id="sess-1",  # 所属会话标识
            status=RunStatus.FAILED,  # 当前状态为失败
            created_at=now,  # 创建时间
            finished_at=now,  # 失败状态已有结束时间
            output=None,  # 失败状态不应携带最终输出
            error_code=ErrorCode.LLM_REQUEST_FAILED,  # 失败状态已有错误码
            error_message=None,  # 失败状态必须有错误描述
        )
    # 断言缺少结束时间时，failed 状态也会被拒绝
    with pytest.raises(ValidationError):
        Run(
            run_id="run-3",  # 运行唯一标识
            session_id="sess-1",  # 所属会话标识
            status=RunStatus.FAILED,  # 当前状态为失败
            created_at=now,  # 创建时间
            finished_at=None,  # 失败状态必须有结束时间
            output=None,  # 失败状态不应携带最终输出
            error_code=ErrorCode.LLM_REQUEST_FAILED,  # 失败状态已有错误码
            error_message="boom",  # 失败状态已有错误描述
        )


# ---------------------------------------------------------------------------
# 事件模型测试
# ---------------------------------------------------------------------------

def test_run_failed_event_payload_matches_contract() -> None:
    """验证 RunFailedEvent 的事件名和 payload 符合契约。"""
    # 构造一个运行失败事件
    event = RunFailedEvent(
        run_id="r1",  # 失败的运行标识
        error_code=ErrorCode.LLM_REQUEST_FAILED,  # 错误码
        message="boom",  # 错误描述信息
    )
    # 验证事件名称属性
    assert event.event_name == "run_failed"
    # 验证 payload 中的 type 字段与事件名称一致
    assert event.to_payload()["type"] == "run_failed"


def test_run_started_event_payload() -> None:
    """验证 RunStartedEvent 的 payload 结构。"""
    event = RunStartedEvent(
        run_id="r1",  # 启动的运行标识
        session_id="sess-1",  # 关联会话标识
    )
    # 验证事件名称和 payload 中的 type
    assert event.event_name == "run_started"
    payload = event.to_payload()
    assert payload["type"] == "run_started"
    assert payload["run_id"] == "r1"
    assert payload["session_id"] == "sess-1"


def test_message_delta_event_payload() -> None:
    """验证 MessageDeltaEvent 的 payload 结构。"""
    event = MessageDeltaEvent(
        run_id="r1",  # 所属运行标识
        content="hello",  # 新增内容片段
    )
    assert event.event_name == "message_delta"
    payload = event.to_payload()
    assert payload["type"] == "message_delta"
    assert payload["run_id"] == "r1"
    assert payload["content"] == "hello"


def test_run_completed_event_payload() -> None:
    """验证 RunCompletedEvent 的 payload 结构。"""
    event = RunCompletedEvent(
        run_id="r1",  # 完成的运行标识
        output="final answer",  # 最终输出内容
    )
    assert event.event_name == "run_completed"
    payload = event.to_payload()
    assert payload["type"] == "run_completed"
    assert payload["run_id"] == "r1"
    assert payload["output"] == "final answer"
    assert "reasoning_content" not in payload  # reasoning_content 仅供内部持久化使用，不应暴露到外部 payload


def test_request_failed_event_payload() -> None:
    """验证 RequestFailedEvent 的 payload 结构。"""
    event = RequestFailedEvent(
        error_code=ErrorCode.SESSION_NOT_FOUND,  # 请求失败错误码
        message="session missing",  # 错误描述
    )
    assert event.event_name == "request_failed"
    payload = event.to_payload()
    assert payload["type"] == "request_failed"
    assert payload["error_code"] == "SESSION_NOT_FOUND"
    assert payload["message"] == "session missing"
    assert payload["run_id"] is None  # 验证未设置 run_id 时 payload 中仍包含该键且值为 None


def test_event_is_abc_or_protocol() -> None:
    """验证所有事件模型都统一继承自 Event。"""
    # 分别构造各类事件实例
    run_started = RunStartedEvent(run_id="r1", session_id="s1")
    message_delta = MessageDeltaEvent(run_id="r1", content="hi")
    run_completed = RunCompletedEvent(run_id="r1", output="done")
    run_failed = RunFailedEvent(
        run_id="r1",
        error_code=ErrorCode.LLM_REQUEST_FAILED,
        message="msg",
    )
    request_failed = RequestFailedEvent(
        error_code=ErrorCode.SESSION_NOT_FOUND,
        message="msg",
    )
    # 断言所有事件实例都是 Event 的子类实例
    assert isinstance(run_started, Event)
    assert isinstance(message_delta, Event)
    assert isinstance(run_completed, Event)
    assert isinstance(run_failed, Event)
    assert isinstance(request_failed, Event)


def test_event_visibility_types_match_contract() -> None:
    """验证内部事件与外部事件的类型边界符合契约。"""
    run_started = RunStartedEvent(run_id="r1", session_id="s1")  # 构造对外开始事件
    message_delta = MessageDeltaEvent(run_id="r1", content="hi")  # 构造对外文本增量事件
    run_completed = RunCompletedEvent(run_id="r1", output="done")  # 构造对外完成事件
    run_failed = RunFailedEvent(  # 构造对外失败事件
        run_id="r1",
        error_code=ErrorCode.LLM_REQUEST_FAILED,
        message="msg",
    )
    request_failed = RequestFailedEvent(  # 构造对外请求失败事件
        error_code=ErrorCode.SESSION_NOT_FOUND,
        message="msg",
    )
    tool_started = ToolUseStartedEvent(  # 构造对外工具开始事件
        run_id="r1",
        tool_name="search",
        tool_call_id="call-1",
        tool_input={"q": "hello"},
    )
    tool_completed = ToolUseCompletedEvent(  # 构造对外工具完成事件
        run_id="r1",
        tool_name="search",
        tool_call_id="call-1",
        is_error=False,
        result="done",
    )
    assistant_with_tools = AssistantWithToolsEvent(  # 构造内部编排事件
        run_id="r1",
        content="thinking",
        reasoning_content="internal reasoning",
        tool_calls=[],
    )

    assert isinstance(run_started, ExternalEvent)  # run_started 应归类为外部协议事件
    assert isinstance(message_delta, ExternalEvent)  # message_delta 应归类为外部协议事件
    assert isinstance(run_completed, ExternalEvent)  # run_completed 应归类为外部协议事件
    assert isinstance(run_failed, ExternalEvent)  # run_failed 应归类为外部协议事件
    assert isinstance(request_failed, ExternalEvent)  # request_failed 应归类为外部协议事件
    assert isinstance(tool_started, ExternalEvent)  # tool_use_started 应归类为外部协议事件
    assert isinstance(tool_completed, ExternalEvent)  # tool_use_completed 应归类为外部协议事件
    assert isinstance(assistant_with_tools, InternalEvent)  # assistant_with_tools 应归类为内部编排事件
    assert not isinstance(assistant_with_tools, ExternalEvent)  # 内部事件不能再被误判为外部事件


# ---------------------------------------------------------------------------
# AppError 测试
# ---------------------------------------------------------------------------

def test_app_error_construction() -> None:
    """验证 AppError 的基本构造和字段正确性。"""
    error = AppError(
        error_code=ErrorCode.SESSION_NOT_FOUND,  # 使用标准错误码枚举
        message="session not found",  # 错误描述
    )
    assert error.error_code == ErrorCode.SESSION_NOT_FOUND
    assert error.message == "session not found"


def test_app_error_serializes_to_dict() -> None:
    """验证 AppError 可以序列化为字典。"""
    error = AppError(
        error_code=ErrorCode.RUN_NOT_FOUND,
        message="run not found",
    )
    data = error.model_dump()
    assert data["error_code"] == "RUN_NOT_FOUND"
    assert data["message"] == "run not found"


def test_error_code_enum_members() -> None:
    """验证 ErrorCode 枚举包含任务约定的全部成员。"""
    # 断言所有约定错误码都在枚举中
    assert ErrorCode.SESSION_NOT_FOUND.value == "SESSION_NOT_FOUND"
    assert ErrorCode.RUN_NOT_FOUND.value == "RUN_NOT_FOUND"
    assert ErrorCode.SESSION_RUN_CONFLICT.value == "SESSION_RUN_CONFLICT"
    assert ErrorCode.LLM_REQUEST_FAILED.value == "LLM_REQUEST_FAILED"
