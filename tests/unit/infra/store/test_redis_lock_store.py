"""RedisLockStore 的单元测试。"""

from __future__ import annotations  # 启用未来注解

import pytest  # 导入 pytest 测试框架

from app.infra.store.redis_lock_store import RedisLockStore  # 导入被测类


@pytest.mark.asyncio  # 标记为异步测试
async def test_lock_store_acquire_returns_true_on_success(fake_redis):
    """测试：当锁未被持有时，acquire 返回 True。"""
    store = RedisLockStore(fake_redis, key_prefix="test")  # 创建 LockStore 实例
    result = await store.acquire("s1", "run-1", ttl_seconds=30)  # 尝试获取锁
    assert result is True  # 断言获取成功


@pytest.mark.asyncio  # 标记为异步测试
async def test_lock_store_acquire_returns_false_when_already_locked(fake_redis):
    """测试：当锁已被持有时，acquire 返回 False。"""
    store = RedisLockStore(fake_redis, key_prefix="test")  # 创建 LockStore 实例
    await store.acquire("s1", "run-1", ttl_seconds=30)  # 先获取锁
    result = await store.acquire("s1", "run-2", ttl_seconds=30)  # 用不同 run_id 再次获取
    assert result is False  # 断言获取失败


@pytest.mark.asyncio  # 标记为异步测试
async def test_lock_store_get_active_run_id_returns_none_when_unlocked(fake_redis):
    """测试：当锁未被持有时，get_active_run_id 返回 None。"""
    store = RedisLockStore(fake_redis, key_prefix="test")  # 创建 LockStore 实例
    result = await store.get_active_run_id("s1")  # 查询活跃 run_id
    assert result is None  # 断言返回 None


@pytest.mark.asyncio  # 标记为异步测试
async def test_lock_store_get_active_run_id_returns_run_id_when_locked(fake_redis):
    """测试：当锁被持有时，get_active_run_id 返回持有者的 run_id。"""
    store = RedisLockStore(fake_redis, key_prefix="test")  # 创建 LockStore 实例
    await store.acquire("s1", "run-1", ttl_seconds=30)  # 获取锁
    result = await store.get_active_run_id("s1")  # 查询活跃 run_id
    assert result == "run-1"  # 断言返回正确的 run_id


@pytest.mark.asyncio  # 标记为异步测试
async def test_lock_store_release_returns_true_on_success(fake_redis):
    """测试：持有者释放锁时，release 返回 True。"""
    store = RedisLockStore(fake_redis, key_prefix="test")  # 创建 LockStore 实例
    await store.acquire("s1", "run-1", ttl_seconds=30)  # 获取锁
    result = await store.release("s1", "run-1")  # 释放锁
    assert result is True  # 断言释放成功


@pytest.mark.asyncio  # 标记为异步测试
async def test_lock_store_release_returns_false_when_not_locked(fake_redis):
    """测试：当锁不存在时，release 返回 False。"""
    store = RedisLockStore(fake_redis, key_prefix="test")  # 创建 LockStore 实例
    result = await store.release("s1", "run-1")  # 尝试释放不存在的锁
    assert result is False  # 断言释放失败


@pytest.mark.asyncio  # 标记为异步测试
async def test_lock_store_releases_only_matching_run_id(fake_redis):
    """测试：只有持有锁的 run_id 才能释放锁（Lua 脚本 owner 校验）。

    这是关键的安全测试：确保非持有者无法释放他人的锁。
    """
    store = RedisLockStore(fake_redis, key_prefix="test")  # 创建 LockStore 实例
    await store.acquire("s1", "run-1", ttl_seconds=30)  # run-1 获取锁
    released = await store.release("s1", "run-2")  # run-2 尝试释放
    assert released is False  # 断言释放失败
    # 验证 run-1 的锁仍然有效
    active_run_id = await store.get_active_run_id("s1")  # 查询活跃 run_id
    assert active_run_id == "run-1"  # 断言锁仍被 run-1 持有


@pytest.mark.asyncio  # 标记为异步测试
async def test_lock_store_key_prefix_isolation(fake_redis):
    """测试：不同 key_prefix 的 store 使用独立的命名空间。"""
    store_a = RedisLockStore(fake_redis, key_prefix="prefix-a")  # 创建 store A
    store_b = RedisLockStore(fake_redis, key_prefix="prefix-b")  # 创建 store B
    await store_a.acquire("s1", "run-1", ttl_seconds=30)  # 在 A 中获取锁
    # 在 B 中应该能获取到锁，因为 key 前缀不同
    result = await store_b.acquire("s1", "run-2", ttl_seconds=30)  # 在 B 中获取锁
    assert result is True  # 断言获取成功


@pytest.mark.asyncio  # 标记为异步测试
async def test_lock_store_extend_returns_true_for_matching_run_id(fake_redis):
    """测试：持有锁的 run_id 可以成功续期。"""
    store = RedisLockStore(fake_redis, key_prefix="test")  # 创建 LockStore 实例
    await store.acquire("s1", "run-1", ttl_seconds=30)  # 先获取锁

    result = await store.extend("s1", "run-1", ttl_seconds=30)  # 续期当前持有的锁

    assert result is True  # 断言续期成功
    active_run_id = await store.get_active_run_id("s1")  # 再次确认 owner 未变化
    assert active_run_id == "run-1"  # 断言锁仍归原持有者


@pytest.mark.asyncio  # 标记为异步测试
async def test_lock_store_extend_returns_false_when_owner_mismatched(fake_redis):
    """测试：非持有者不能续期别人的锁。"""
    store = RedisLockStore(fake_redis, key_prefix="test")  # 创建 LockStore 实例
    await store.acquire("s1", "run-1", ttl_seconds=30)  # 先由 run-1 获取锁

    result = await store.extend("s1", "run-2", ttl_seconds=30)  # run-2 尝试续期

    assert result is False  # 断言续期失败
    active_run_id = await store.get_active_run_id("s1")  # 再次确认锁 owner
    assert active_run_id == "run-1"  # 断言原锁仍归 run-1 持有


@pytest.mark.asyncio  # 标记为异步测试
async def test_lock_store_extend_returns_false_when_lock_missing(fake_redis):
    """测试：锁不存在时续期失败。"""
    store = RedisLockStore(fake_redis, key_prefix="test")  # 创建 LockStore 实例

    result = await store.extend("s1", "run-1", ttl_seconds=30)  # 尝试续期不存在的锁

    assert result is False  # 断言续期失败
