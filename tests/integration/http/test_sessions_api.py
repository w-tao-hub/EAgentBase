"""会话接口 HTTP 集成测试。

测试 POST /sessions 和 GET /sessions/{session_id} 接口。
"""

from __future__ import annotations  # 启用未来注解

import pytest  # 导入 pytest 测试框架
import pytest_asyncio  # 导入 pytest 异步支持
from httpx import ASGITransport, AsyncClient  # 导入异步 HTTP 客户端

import fakeredis.aioredis  # 导入 fakeredis 异步实现

from app.bootstrap.container import Container  # 导入容器，便于 patch Redis 创建点
from app.bootstrap.factory import bootstrap_app  # 导入公开启动入口，复用真实 bootstrap 路径


@pytest_asyncio.fixture  # 定义异步夹具
async def async_client(monkeypatch):
    """提供注入假依赖的异步 HTTP 测试客户端。

    使用 fakeredis 替代真实 Redis 依赖，
    避免测试依赖外部服务。
    """
    # 创建 fakeredis 实例
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    # 通过应用工厂创建 FastAPI 实例，注入假依赖
    monkeypatch.setattr(Container, "_create_redis", staticmethod(lambda settings: redis))  # 拦截主 Redis 创建，改为返回 fakeredis
    monkeypatch.setattr(Container, "_create_pubsub_redis", staticmethod(lambda settings: redis))  # 拦截 pubsub Redis 创建，复用同一 fakeredis 替身
    app = bootstrap_app()

    # 构造异步测试客户端
    transport = ASGITransport(app=app)  # 创建 ASGI 传输层
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client  # 提供客户端给测试函数

    # 清理：关闭 fakeredis 连接
    await redis.aclose()


@pytest.mark.asyncio  # 标记为异步测试
async def test_create_session_returns_http_200_with_session_id(async_client):
    """测试创建会话接口返回 200 和有效的 session_id。"""
    # 调用 POST /sessions 创建会话
    response = await async_client.post("/sessions")

    # 验证返回状态码为 200
    assert response.status_code == 200

    # 验证返回的 JSON 中包含 session_id 字段
    data = response.json()
    assert "session_id" in data  # 必须包含 session_id
    assert len(data["session_id"]) > 0  # session_id 不能为空


@pytest.mark.asyncio  # 标记为异步测试
async def test_get_session_returns_session_view(async_client):
    """测试查询会话接口返回正确的会话视图。"""
    # 先创建一个会话
    create_response = await async_client.post("/sessions")
    session_id = create_response.json()["session_id"]  # 获取创建的 session_id

    # 查询该会话
    response = await async_client.get(f"/sessions/{session_id}")

    # 验证返回状态码为 200
    assert response.status_code == 200

    # 验证返回数据
    data = response.json()
    assert data["session_id"] == session_id  # session_id 匹配
    assert "agent_id" in data  # 包含 agent_id
    assert "created_at" in data  # 包含创建时间
    assert "message_count" in data  # 包含消息数量


@pytest.mark.asyncio  # 标记为异步测试
async def test_get_missing_session_returns_http_200_with_request_failed(async_client):
    """测试查询不存在的会话返回 200 和 request_failed 类型。"""
    # 查询一个不存在的会话
    response = await async_client.get("/sessions/missing-session-id")

    # 验证返回状态码为 200（业务错误不使用 HTTP 错误码）
    assert response.status_code == 200

    # 验证返回的错误类型为 request_failed
    assert response.json()["type"] == "request_failed"
