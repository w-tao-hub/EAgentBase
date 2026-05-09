"""pytest 共享测试夹具定义。"""

from __future__ import annotations

import pytest
import fakeredis.aioredis


@pytest.fixture
async def fake_redis():
    """提供 fakeredis 异步 Redis 客户端实例。"""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield redis
    finally:
        await redis.aclose()
