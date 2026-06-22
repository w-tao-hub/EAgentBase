"""RedisRunCancelBus 单元测试。"""

from __future__ import annotations

import asyncio

import pytest

from app.infra.store.redis_run_cancel_bus import RedisRunCancelBus


@pytest.mark.asyncio
async def test_redis_run_cancel_bus_publishes_and_listens_cancelled_run_id(fake_redis):
    """测试取消广播适配器会产出被取消的 run_id。"""
    bus = RedisRunCancelBus(fake_redis)
    received: list[str] = []

    async def consume_one() -> None:
        async for run_id in bus.listen_cancelled_run_ids():
            received.append(run_id)
            break

    task = asyncio.create_task(consume_one())
    await asyncio.sleep(0)

    await bus.publish_cancel("run-1")
    await asyncio.wait_for(task, timeout=1)
    await bus.aclose()

    assert received == ["run-1"]


@pytest.mark.asyncio
async def test_redis_run_cancel_bus_ignores_non_cancel_payload(fake_redis):
    """测试非 cancel 消息不会被取消监听器产出。"""
    bus = RedisRunCancelBus(fake_redis)
    received: list[str] = []

    async def consume_until_cancelled() -> None:
        async for run_id in bus.listen_cancelled_run_ids():
            received.append(run_id)

    task = asyncio.create_task(consume_until_cancelled())
    await asyncio.sleep(0)

    await fake_redis.publish("run_cancel:run-ignored", "other")
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await bus.aclose()

    assert received == []


@pytest.mark.asyncio
async def test_redis_run_cancel_bus_aclose_stops_active_listener(fake_redis):
    """测试主动关闭 bus 会让正在监听的消费协程自然退出。"""
    bus = RedisRunCancelBus(fake_redis)
    received: list[str] = []

    async def consume_until_closed() -> None:
        async for run_id in bus.listen_cancelled_run_ids():
            received.append(run_id)

    task = asyncio.create_task(consume_until_closed())
    await asyncio.sleep(0)

    await bus.aclose()
    await asyncio.wait_for(task, timeout=1)

    assert received == []
