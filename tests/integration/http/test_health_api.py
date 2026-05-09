"""健康检查 HTTP 集成测试。

测试 `/health/ready` 的就绪探针行为。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import fakeredis.aioredis

from app.bootstrap.container import Container
from app.bootstrap.factory import bootstrap_app


@pytest_asyncio.fixture
async def health_client(monkeypatch):
    """提供注入 fakeredis 的健康检查测试客户端。"""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(Container, "_create_redis", staticmethod(lambda settings: redis))
    monkeypatch.setattr(Container, "_create_pubsub_redis", staticmethod(lambda settings: redis))

    app = bootstrap_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, redis

    await redis.aclose()


@pytest.mark.asyncio
async def test_health_ready_returns_ready_when_probe_succeeds(health_client):
    """验证 readiness 探测成功时返回 ready。"""
    client, _redis = health_client

    response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {
            "redis": "ok",
        },
    }


@pytest.mark.asyncio
async def test_health_ready_returns_503_when_probe_fails(health_client, monkeypatch):
    """验证 readiness 探测失败时返回 503。"""
    client, redis = health_client

    async def broken_ping() -> None:
        raise RuntimeError("redis down")

    monkeypatch.setattr(redis, "ping", broken_ping)

    response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {
            "redis": "error: redis down",
        },
    }
