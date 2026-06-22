"""会话接口 HTTP 集成测试。

测试 POST /sessions 和 GET /sessions/{session_id} 接口。
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
async def test_create_session_returns_http_200_with_session_id(async_client):
    """测试创建会话接口返回 200 和有效的 session_id。"""
    response = await async_client.post("/sessions")

    assert response.status_code == 200

    data = response.json()
    assert "session_id" in data
    assert len(data["session_id"]) > 0


@pytest.mark.asyncio
async def test_create_session_can_bind_plan_master_agent(async_client):
    """测试创建会话时可以绑定 plan 主代理。"""
    response = await async_client.post("/sessions", json={"master_agent_name": "plan"})

    assert response.status_code == 200

    data = response.json()
    assert data["agent_id"] == "plan"
    assert "session_id" in data


@pytest.mark.asyncio
async def test_create_session_returns_request_failed_for_unknown_master_agent(async_client):
    """测试创建会话时未知主代理返回 request_failed。"""
    response = await async_client.post("/sessions", json={"master_agent_name": "ghost"})

    assert response.status_code == 200

    data = response.json()
    assert data["type"] == "request_failed"
    assert data["error_code"] == "UNKNOWN_MASTER_AGENT"


@pytest.mark.asyncio
async def test_get_session_returns_session_view(async_client):
    """测试查询会话接口返回正确的会话视图。"""
    create_response = await async_client.post("/sessions")
    session_id = create_response.json()["session_id"]

    response = await async_client.get(f"/sessions/{session_id}")

    assert response.status_code == 200

    data = response.json()
    assert data["session_id"] == session_id
    assert "agent_id" in data
    assert "created_at" in data
    assert "message_count" in data


@pytest.mark.asyncio
async def test_get_missing_session_returns_http_200_with_request_failed(async_client):
    """测试查询不存在的会话返回 200 和 request_failed 类型。"""
    response = await async_client.get("/sessions/missing-session-id")

    assert response.status_code == 200
    assert response.json()["type"] == "request_failed"
