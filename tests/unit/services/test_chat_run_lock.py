"""ChatRunLockScope 单元测试。"""

from __future__ import annotations  # 启用未来注解

import asyncio  # 导入 asyncio，用于等待后台心跳触发

import pytest  # 导入 pytest，用于异步测试与异常断言

from app.infra.store.redis_lock_store import RedisLockStore  # 导入 Redis 锁存储实现
from app.services.chat_run_lock import (  # 导入待测锁作用域
    ChatRunLockHeartbeatLostError,
    ChatRunLockNotAcquiredError,
    ChatRunLockScope,
)


@pytest.mark.asyncio
async def test_chat_run_lock_scope_releases_lock_when_scope_exits(fake_redis):
    """测试正常退出作用域后会释放锁。"""
    lock_store = RedisLockStore(fake_redis, key_prefix="test")  # 创建测试用锁存储

    async with ChatRunLockScope(
        lock_store=lock_store,
        session_id="session-1",
        run_id="run-1",
        ttl_seconds=30,
    ):
        active_run_id = await lock_store.get_active_run_id("session-1")  # 在作用域内应能读到当前锁 owner
        assert active_run_id == "run-1"  # 验证锁已成功获取

    active_run_id = await lock_store.get_active_run_id("session-1")  # 退出作用域后再次读取锁
    assert active_run_id is None  # 验证锁已被释放


@pytest.mark.asyncio
async def test_chat_run_lock_scope_releases_lock_when_scope_raises(fake_redis):
    """测试作用域内部抛异常时仍会释放锁。"""
    lock_store = RedisLockStore(fake_redis, key_prefix="test")  # 创建测试用锁存储

    with pytest.raises(RuntimeError):  # 验证业务异常本身不会被锁作用域吞掉
        async with ChatRunLockScope(
            lock_store=lock_store,
            session_id="session-1",
            run_id="run-1",
            ttl_seconds=30,
        ):
            raise RuntimeError("boom")  # 主动抛出业务异常，模拟作用域内部失败

    active_run_id = await lock_store.get_active_run_id("session-1")  # 作用域异常退出后再次读取锁
    assert active_run_id is None  # 验证锁仍然会被释放


@pytest.mark.asyncio
async def test_chat_run_lock_scope_raises_when_lock_already_held(fake_redis):
    """测试锁已被占用时会抛出受控异常，且不会误释放原 owner。"""
    lock_store = RedisLockStore(fake_redis, key_prefix="test")  # 创建测试用锁存储
    await lock_store.acquire("session-1", "existing-run", ttl_seconds=30)  # 先由其他 run 占用锁

    with pytest.raises(ChatRunLockNotAcquiredError):  # 验证获取失败时抛出受控异常
        async with ChatRunLockScope(
            lock_store=lock_store,
            session_id="session-1",
            run_id="new-run",
            ttl_seconds=30,
        ):
            pass  # 该分支不会执行，因为进入作用域前就应失败

    active_run_id = await lock_store.get_active_run_id("session-1")  # 再次读取锁 owner
    assert active_run_id == "existing-run"  # 验证原锁持有者未被错误释放


@pytest.mark.asyncio
async def test_chat_run_lock_scope_starts_heartbeat_after_acquire(fake_redis):
    """测试作用域持锁期间会启动后台续期。"""
    lock_store = RedisLockStore(fake_redis, key_prefix="test")  # 创建测试用锁存储
    heartbeat_called = asyncio.Event()  # 用于标记后台续期已触发

    async def recording_extend(session_id: str, run_id: str, ttl_seconds: int) -> bool:
        """记录一次续期调用。"""
        heartbeat_called.set()  # 标记后台续期已发生
        return True  # 返回成功，保持作用域继续持锁

    lock_store.extend = recording_extend  # 替换为记录型续期函数

    async with ChatRunLockScope(
        lock_store=lock_store,
        session_id="session-1",
        run_id="run-1",
        ttl_seconds=2,
    ):
        await asyncio.wait_for(heartbeat_called.wait(), timeout=2.0)  # 等待后台续期至少触发一次


@pytest.mark.asyncio
async def test_chat_run_lock_scope_raises_when_heartbeat_lost(fake_redis):
    """测试后台续期失败时会让当前作用域失败退出。"""
    lock_store = RedisLockStore(fake_redis, key_prefix="test")  # 创建测试用锁存储

    async def failing_extend(session_id: str, run_id: str, ttl_seconds: int) -> bool:
        """模拟续期失败。"""
        return False  # 返回 False 表示当前 run 已失去锁 owner

    lock_store.extend = failing_extend  # 替换为失败续期函数

    with pytest.raises(ChatRunLockHeartbeatLostError):  # 验证最终抛出受控失锁异常
        async with ChatRunLockScope(
            lock_store=lock_store,
            session_id="session-1",
            run_id="run-1",
            ttl_seconds=2,
        ):
            await asyncio.sleep(1.2)  # 等待首次心跳触发并让后台标记失锁
