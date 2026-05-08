"""健康检查 HTTP 集成测试。

测试 `/health/ready` 的就绪探针行为。
"""

from __future__ import annotations  # 启用未来注解

import pytest  # 导入 pytest 测试框架
import pytest_asyncio  # 导入 pytest 异步支持
from httpx import ASGITransport, AsyncClient  # 导入异步 HTTP 客户端

import fakeredis.aioredis  # 导入 fakeredis 异步实现

from app.bootstrap.container import Container  # 导入容器，便于 patch Redis 创建点
from app.bootstrap.factory import bootstrap_app  # 导入公开启动入口，复用真实 bootstrap 路径


@pytest_asyncio.fixture  # 定义异步夹具
async def health_client(monkeypatch):
    """提供注入 fakeredis 的健康检查测试客户端。"""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)  # 创建 fakeredis 实例
    monkeypatch.setattr(Container, "_create_redis", staticmethod(lambda settings: redis))  # 拦截主 Redis 创建，改为返回 fakeredis
    monkeypatch.setattr(Container, "_create_pubsub_redis", staticmethod(lambda settings: redis))  # 拦截 pubsub Redis 创建，复用同一 fakeredis 替身

    app = bootstrap_app()  # 创建应用实例，复用真实 bootstrap 路径
    transport = ASGITransport(app=app)  # 创建 ASGI 传输层

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, redis  # 同时返回客户端和 fakeredis，便于单测定向 patch ping

    await redis.aclose()  # 测试结束后关闭 fakeredis 连接


@pytest.mark.asyncio
async def test_health_ready_returns_ready_when_probe_succeeds(health_client):
    """验证 readiness 探测成功时返回 ready。"""
    client, _redis = health_client  # 拆包测试客户端与 fakeredis 实例

    response = await client.get("/health/ready")  # 调用 readiness 接口

    assert response.status_code == 200  # 验证探测成功时返回 200
    assert response.json() == {  # 验证返回体保持当前 ready 协议
        "status": "ready",
        "checks": {
            "redis": "ok",
        },
    }


@pytest.mark.asyncio
async def test_health_ready_returns_503_when_probe_fails(health_client, monkeypatch):
    """验证 readiness 探测失败时返回 503。"""
    client, redis = health_client  # 拆包测试客户端与 fakeredis 实例

    async def broken_ping() -> None:
        """模拟 Redis 不可用，驱动 readiness 走失败分支。"""
        raise RuntimeError("redis down")  # 显式抛错，模拟底层依赖不可用

    monkeypatch.setattr(redis, "ping", broken_ping)  # 拦截 fakeredis ping，模拟依赖异常

    response = await client.get("/health/ready")  # 调用 readiness 接口

    assert response.status_code == 503  # 验证探测失败时返回 503
    assert response.json() == {  # 验证返回体保持当前 not_ready 协议
        "status": "not_ready",
        "checks": {
            "redis": "error: redis down",
        },
    }
