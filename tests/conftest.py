"""pytest 共享测试夹具定义。"""

from __future__ import annotations  # 启用未来注解

import pytest  # 导入 pytest 测试框架
import fakeredis.aioredis  # 导入 fakeredis 异步 Redis 实现


@pytest.fixture  # 定义 pytest 夹具
async def fake_redis():
    """提供 fakeredis 异步 Redis 客户端实例。

    该夹具为每个测试提供一个独立的、内存中的 Redis 实例，
    使用 decode_responses=True 确保返回字符串而非字节。
    """
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)  # 创建 FakeRedis 实例
    try:
        yield redis  # 将实例提供给测试函数
    finally:
        await redis.aclose()  # 测试结束后关闭连接
