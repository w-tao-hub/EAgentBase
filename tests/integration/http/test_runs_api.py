"""运行接口 HTTP 集成测试。

测试 GET /runs/{run_id} 接口。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import fakeredis.aioredis

from app.bootstrap.container import Container
from app.bootstrap.factory import bootstrap_app


@pytest_asyncio.fixture
async def async_client(monkeypatch):
    """提供注入假依赖的异步 HTTP 测试客户端。"""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    monkeypatch.setattr(Container, "_create_redis", staticmethod(lambda settings: redis))
    monkeypatch.setattr(Container, "_create_pubsub_redis", staticmethod(lambda settings: redis))
    app = bootstrap_app()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    await redis.aclose()


@pytest.mark.asyncio
async def test_get_missing_run_returns_http_200_with_request_failed(async_client):
    """测试查询不存在的运行返回 200 和 request_failed 类型。"""
    response = await async_client.get("/runs/missing")

    assert response.status_code == 200
    assert response.json()["type"] == "request_failed"
