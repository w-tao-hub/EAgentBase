"""RunControlService 单元测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.services.run_control_service import RunControlService
from app.infra.store.redis_run_store import RedisRunStore
from app.core.models.run import Run, RunStatus
from app.core.models.error import ErrorCode


@pytest.fixture  # 定义 pytest 夹具
async def run_service(fake_redis):  # RunControlService 夹具
    """提供配置好的 RunControlService 实例。"""
    run_store = RedisRunStore(fake_redis, key_prefix="test")  # 创建 Run 存储
    service = RunControlService(run_store=run_store, chat_service=None)  # 创建服务实例，chat_service 可传 None（当前测试仅覆盖 get_run）
    return service  # 返回服务实例


@pytest.mark.asyncio  # 标记异步测试
async def test_get_run_returns_run_for_existing_id(run_service):  # 测试获取存在的 Run
    """测试获取存在的 Run 应返回 Run 对象。"""
    # 创建一个 Run
    run = Run(  # 构造 Run 实例
        run_id="run-1",
        session_id="session-1",
        status=RunStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
    )
    await run_service._run_store.create_run(run)  # 创建 Run 记录

    # 获取 Run
    result = await run_service.get_run("run-1")  # 查询 Run
    assert isinstance(result, Run)  # 验证返回的是 Run 对象
    assert result.run_id == "run-1"  # 验证 Run ID 正确
    assert result.session_id == "session-1"  # 验证 Session ID 正确
    assert result.status == RunStatus.RUNNING  # 验证状态正确


@pytest.mark.asyncio  # 标记异步测试
async def test_get_run_returns_error_for_missing_id(run_service):  # 测试获取不存在的 Run
    """测试获取不存在的 Run 应返回 AppError。"""
    result = await run_service.get_run("non-existent-id")  # 查询不存在的 Run

    # 验证返回的是 AppError
    from app.core.models.error import AppError
    assert isinstance(result, AppError)  # 验证返回的是错误对象
    assert result.error_code == ErrorCode.RUN_NOT_FOUND  # 验证错误码正确
    assert "not found" in result.message.lower() or "不存在" in result.message  # 验证错误消息包含"not found"或"不存在"


@pytest.mark.asyncio  # 标记异步测试
async def test_get_run_returns_completed_run_with_output(run_service):  # 测试获取已完成的 Run
    """测试获取已完成的 Run 应包含 output 字段。"""
    # 创建一个已完成的 Run
    run = Run(  # 构造 Run 实例
        run_id="run-2",
        session_id="session-1",
        status=RunStatus.COMPLETED,
        created_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        output="This is the final answer.",
    )
    await run_service._run_store.create_run(run)  # 创建 Run 记录

    # 获取 Run
    result = await run_service.get_run("run-2")  # 查询 Run
    assert isinstance(result, Run)  # 验证返回的是 Run 对象
    assert result.status == RunStatus.COMPLETED  # 验证状态为已完成
    assert result.output == "This is the final answer."  # 验证输出内容正确
    assert result.finished_at is not None  # 验证完成时间不为空


@pytest.mark.asyncio  # 标记异步测试
async def test_get_run_returns_failed_run_with_error(run_service):  # 测试获取失败的 Run
    """测试获取失败的 Run 应包含错误信息。"""
    # 创建一个失败的 Run
    run = Run(  # 构造 Run 实例
        run_id="run-3",
        session_id="session-1",
        status=RunStatus.FAILED,
        created_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        error_code=ErrorCode.LLM_REQUEST_FAILED,
        error_message="LLM request timeout",
    )
    await run_service._run_store.create_run(run)  # 创建 Run 记录

    # 获取 Run
    result = await run_service.get_run("run-3")  # 查询 Run
    assert isinstance(result, Run)  # 验证返回的是 Run 对象
    assert result.status == RunStatus.FAILED  # 验证状态为失败
    assert result.error_code == ErrorCode.LLM_REQUEST_FAILED  # 验证错误码正确
    assert result.error_message == "LLM request timeout"  # 验证错误消息正确
