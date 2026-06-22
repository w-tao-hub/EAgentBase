"""聊天 SSE 接口 HTTP 集成测试。

测试 POST /chat 接口的 SSE 流式响应行为。
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import fakeredis.aioredis

from app.bootstrap.container import Container
from app.bootstrap.factory import bootstrap_app
from app.core.models.execution_context import ExecutionContext
from app.core.models.tool import Tool, ToolResult
from app.infra.store.redis_session_store import RedisSessionStore
import app.infra.llm.litellm_adapter as litellm_adapter_module
from tests.fakes import FakeLLMAdapter
from tests.helpers.sse import collect_sse_events
from app.core.models.llm_chunk import LLMChunk


class StubLargeResultTool(Tool):
    """返回超大结果的只读工具替身。"""

    @property
    def name(self) -> str:
        """返回固定工具名。"""
        return "mcp_large_result"

    @property
    def description(self) -> str:
        """返回固定工具描述。"""
        return "测试用超大结果工具"

    @property
    def input_schema(self) -> dict:
        """返回最小输入 schema。"""
        return {"type": "object", "additionalProperties": False}

    def is_read_only(self) -> bool:
        """声明该测试工具为只读。"""
        return True

    async def call(self, input: dict, context: ExecutionContext) -> ToolResult:
        """返回超过预览阈值的超大结果。"""
        return ToolResult(content="超大输出" * 4000, is_error=False)


class StubMCPClientManager:
    """最小 MCP 管理器替身。"""

    def __init__(self, tools: list[Tool]) -> None:
        """保存要暴露给容器的工具列表。"""
        self._tools = tools

    def list_tools(self) -> list[Tool]:
        """返回工具列表。"""
        return list(self._tools)

    async def aclose(self) -> None:
        """提供最小关闭接口，满足容器生命周期管理。"""
        return None


class SlowStreamingLLMAdapter:
    """慢速流式 LLM 适配器替身。用于测试客户端并发断连后的清理与终态收敛。"""

    def __init__(self, total_chunks: int = 500, delay_seconds: float = 0.01) -> None:
        self._total_chunks = total_chunks
        self._delay_seconds = delay_seconds

    async def stream_completion(
        self,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        api_key: str | None = None,
        tools: list[dict] | None = None,
        reasoning_effort: str | None = None,
    ):
        """持续输出文本 chunk，直到被上游取消。"""
        del model, messages, temperature, api_key, tools, reasoning_effort
        for index in range(self._total_chunks):
            await asyncio.sleep(self._delay_seconds)
            yield LLMChunk(content=f"{index}\n")


def _patch_container_redis(monkeypatch, redis) -> None:
    """统一替换容器中的主 Redis 与 pubsub Redis 创建点。"""
    monkeypatch.setattr(Container, "_create_redis", staticmethod(lambda settings: redis))
    monkeypatch.setattr(Container, "_create_pubsub_redis", staticmethod(lambda settings: redis))


async def _read_first_sse_event(response) -> tuple[str, dict]:
    """从流式响应中读取第一个完整 SSE 事件。"""
    event_name = ""
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if line.startswith("event:"):
            event_name = line[len("event:"):].strip()
            continue
        if line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())
            continue
        if line == "" and event_name and data_lines:
            import json

            return event_name, json.loads("".join(data_lines))
    raise AssertionError("未读取到完整 SSE 事件")


async def _wait_run_cancelled(client: AsyncClient, run_id: str, timeout_seconds: float = 5.0) -> dict:
    """轮询等待指定 run 收敛为 cancelled。"""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    latest_payload: dict | None = None
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(f"/runs/{run_id}")
        latest_payload = response.json()
        if latest_payload.get("status") == "cancelled":
            return latest_payload
        await asyncio.sleep(0.05)
    raise AssertionError(f"run 未在超时内收敛为 cancelled: run_id={run_id}, payload={latest_payload}")


@pytest_asyncio.fixture
async def chat_client(monkeypatch):
    """提供注入假依赖的异步 HTTP 测试客户端。"""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    fake_llm_adapter = FakeLLMAdapter(chunks=[
        LLMChunk(content="Hello"),
        LLMChunk(content=" world"),
    ])
    monkeypatch.setattr(
        litellm_adapter_module,
        "LiteLLMAdapter",
        lambda *args, **kwargs: fake_llm_adapter,
    )
    _patch_container_redis(monkeypatch, redis)

    app = bootstrap_app()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    await redis.aclose()


@pytest.mark.asyncio
async def test_chat_sse_emits_expected_event_order(chat_client):
    """测试聊天 SSE 事件按照预期顺序发出。"""
    create_resp = await chat_client.post("/sessions")
    session_id = create_resp.json()["session_id"]

    events = await collect_sse_events(
        chat_client,
        "/chat",
        json={"session_id": session_id, "master_agent_name": "default", "message": "Hi"},
    )

    assert len(events) > 0

    event_names = [e["event"] for e in events]
    assert event_names[0] == "run_started"
    assert event_names[-1] == "run_completed"

    delta_events = [e for e in events if e["event"] == "message_delta"]
    assert len(delta_events) > 0


@pytest.mark.asyncio
async def test_chat_sse_with_missing_session_returns_request_failed(chat_client):
    """测试聊天时使用不存在的 session_id 返回 request_failed SSE 事件。"""
    events = await collect_sse_events(
        chat_client,
        "/chat",
        json={"session_id": "nonexistent-session", "master_agent_name": "default", "message": "Hi"},
    )

    assert len(events) > 0
    assert events[0]["event"] == "request_failed"
    assert events[0]["data"]["error_code"] == "SESSION_NOT_FOUND"


@pytest.mark.asyncio  # 标记为异步测试
async def test_chat_validation_error_response_omits_details(chat_client):
    """测试 `/chat` 参数校验失败时不再返回 `details` 字段。"""
    # 发送缺少 master_agent_name 的请求，触发必填字段校验。
    response = await chat_client.post(
        "/chat",
        json={"session_id": "session-1", "message": "1"},
    )

    # 仍然保持 422，但响应体只保留面向调用方需要的摘要信息。
    assert response.status_code == 422
    payload = response.json()
    assert payload["error"] == "VALIDATION_ERROR"
    assert "master_agent_name" in payload["message"]
    assert "details" not in payload


@pytest.mark.asyncio
async def test_chat_sse_with_metadata(chat_client):
    """测试聊天时传递 metadata 参数不影响正常事件流。"""
    create_resp = await chat_client.post("/sessions")
    session_id = create_resp.json()["session_id"]

    events = await collect_sse_events(
        chat_client,
        "/chat",
        json={
            "session_id": session_id,
            "master_agent_name": "default",
            "message": "Hi",
            "metadata": {"source": "test", "version": "1.0"},
        },
    )

    assert len(events) > 0
    event_names = [e["event"] for e in events]
    assert event_names[0] == "run_started"
    assert event_names[-1] == "run_completed"


@pytest.mark.asyncio
async def test_chat_multi_turn_conversation(chat_client):
    """测试多轮对话：多次发送消息，验证消息计数递增。"""
    create_resp = await chat_client.post("/sessions")
    session_id = create_resp.json()["session_id"]

    events1 = await collect_sse_events(
        chat_client,
        "/chat",
        json={"session_id": session_id, "master_agent_name": "default", "message": "Hello"},
    )
    assert len(events1) > 0
    assert events1[-1]["event"] == "run_completed"

    session_resp = await chat_client.get(f"/sessions/{session_id}")
    session_data = session_resp.json()
    assert session_data["message_count"] == 2

    events2 = await collect_sse_events(
        chat_client,
        "/chat",
        json={"session_id": session_id, "master_agent_name": "default", "message": "How are you?"},
    )
    assert len(events2) > 0
    assert events2[-1]["event"] == "run_completed"

    session_resp2 = await chat_client.get(f"/sessions/{session_id}")
    session_data2 = session_resp2.json()
    assert session_data2["message_count"] == 4


@pytest.mark.asyncio  # 标记为异步测试
async def test_chat_sse_with_tool_call_flow(monkeypatch):
    """测试完整工具调用流程的 SSE 事件序列。

    验证当 LLM 返回 tool_calls 时，事件流中包含：
    run_started -> tool_use_started -> tool_use_completed -> message_delta -> run_completed
    """
    # 创建 fakeredis 实例
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    # 创建假的 LLM 适配器，模拟两轮调用：
    # 第一轮返回工具调用，第二轮返回正常文本。
    fake_llm_adapter = FakeLLMAdapter(
        turn_chunks=[
            [
                LLMChunk(
                    tool_calls=[
                        {
                            "index": 0,
                            "id": "call_1",
                            "function_name": "unknown_test_tool",
                            "arguments": "{}",
                        }
                    ]
                ),
            ],
            [
                LLMChunk(content="处理完成"),
            ],
        ]
    )
    monkeypatch.setattr(  # 拦截真实适配器创建，改为返回测试替身
        litellm_adapter_module,
        "LiteLLMAdapter",
        lambda *args, **kwargs: fake_llm_adapter,
    )
    _patch_container_redis(monkeypatch, redis)  # 统一拦截主 Redis 与 pubsub Redis 创建，避免测试触碰真实 Redis

    # 通过应用工厂创建 FastAPI 实例，注入假依赖
    app = bootstrap_app()

    # 构造异步测试客户端
    transport = ASGITransport(app=app)  # 创建 ASGI 传输层
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 先创建一个会话
        create_resp = await client.post("/sessions")  # 创建会话
        session_id = create_resp.json()["session_id"]  # 获取 session_id

        # 发送聊天请求并收集 SSE 事件
        events = await collect_sse_events(
            client,  # 测试客户端
            "/chat",  # 请求 URL
            json={"session_id": session_id, "master_agent_name": "default", "message": "调用工具"},  # 请求体
        )

        # 验证事件名称序列
        event_names = [e["event"] for e in events]  # 提取事件名称列表

        # 验证包含工具调用开始和完成事件
        assert event_names[0] == "run_started"  # 第一个事件是 run_started
        assert "tool_use_started" in event_names  # 有工具调用开始事件
        assert "tool_use_completed" in event_names  # 有工具调用完成事件

        # 验证 tool_use_completed 是错误结果（未知工具）
        tool_completed_events = [e for e in events if e["event"] == "tool_use_completed"]  # 过滤工具完成事件
        assert len(tool_completed_events) == 1  # 只有一个工具完成事件
        assert tool_completed_events[0]["data"]["is_error"] is True  # 是错误结果
        assert "未知工具" in tool_completed_events[0]["data"]["result"]  # 错误信息包含"未知工具"

        # 验证第二轮有 message_delta 和 run_completed
        assert "message_delta" in event_names  # 有消息增量事件
        assert event_names[-1] == "run_completed"  # 最后一个事件是 run_completed

    # 清理：关闭 fakeredis 连接
    await redis.aclose()


@pytest.mark.asyncio  # 标记为异步测试
async def test_chat_sse_persists_large_tool_result_preview_and_reuses_placeholder(monkeypatch):
    """测试超大工具结果会在 HTTP 链路中统一替换为占位文本，并把完整正文持久化到 Redis。"""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)  # 创建 fakeredis，隔离本测试 Redis 状态。
    fake_llm_adapter = FakeLLMAdapter(  # 构造两轮 LLM 返回：第一轮发起工具调用，第二轮输出完成文本。
        turn_chunks=[
            [
                LLMChunk(
                    tool_calls=[
                        {
                            "index": 0,
                            "id": "call_1",
                            "function_name": "mcp_large_result",
                            "arguments": "{}",
                        }
                    ]
                ),
            ],
            [
                LLMChunk(content="处理完成"),
            ],
        ]
    )
    monkeypatch.setattr(  # 拦截真实适配器创建，改为返回测试替身。
        litellm_adapter_module,
        "LiteLLMAdapter",
        lambda *args, **kwargs: fake_llm_adapter,
    )
    _patch_container_redis(monkeypatch, redis)  # 统一拦截主 Redis 与 pubsub Redis 创建，避免测试触碰真实 Redis。
    monkeypatch.setattr(  # 拦截 MCP 管理器创建，注入返回超大结果的测试工具。
        Container,
        "_create_mcp_client_manager",
        staticmethod(lambda settings: StubMCPClientManager([StubLargeResultTool()])),
    )

    app = bootstrap_app()  # 走真实 bootstrap 路径创建应用。
    transport = ASGITransport(app=app)  # 构造 ASGI 传输层。
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post("/sessions")  # 先创建一个会话。
        session_id = create_resp.json()["session_id"]  # 读取会话 ID。

        events = await collect_sse_events(  # 发起聊天请求并收集 SSE 事件。
            client,
            "/chat",
            json={"session_id": session_id, "master_agent_name": "default", "message": "调用超大工具"},
        )

        tool_completed_event = next(event for event in events if event["event"] == "tool_use_completed")  # 读取工具完成事件。
        placeholder = tool_completed_event["data"]["result"]  # 提取占位文本。
        persisted_key = placeholder.split("（", 1)[1].split("）", 1)[0]  # 从占位文本中解析 Redis key。

        assert "<persisted-output>" in placeholder  # SSE 中应看到占位文本。
        assert "QueryToolResult" in placeholder  # 占位文本应提示通过 QueryToolResult 查询完整正文。
        assert persisted_key.startswith("agent:tool_result:")  # key 应符合默认 Redis 前缀命名空间。
        assert await redis.exists(persisted_key) == 1  # 完整正文应已被写入 Redis。

        session_store = RedisSessionStore(redis, key_prefix="agent")  # 使用默认前缀读取会话历史。
        messages = await session_store.list_messages(session_id)  # 读取当前会话全部消息。
        tool_messages = [message for message in messages if message.role == "tool"]  # 筛出 tool 历史消息。

        assert len(tool_messages) == 1  # 当前会话应只产生一条工具结果消息。
        assert tool_messages[0].content == placeholder  # 会话历史中的 tool 消息也应是相同占位文本。

        second_turn_messages = fake_llm_adapter.last_call["messages"]  # 第二轮模型上下文应保存在最后一次调用参数中。
        assert second_turn_messages[-1]["role"] == "tool"  # 最后一条上下文消息应为 tool 结果。
        assert second_turn_messages[-1]["content"] == placeholder  # 下一轮模型上下文也应看到相同占位文本。

    await redis.aclose()  # 关闭 fakeredis 连接，释放测试资源。


@pytest.mark.asyncio  # 标记为异步测试
async def test_chat_sse_can_route_to_plan_master_agent(chat_client):
    """验证 plan 主代理会话可以完成显式路由聊天。"""
    create_resp = await chat_client.post("/sessions", json={"master_agent_name": "plan"})  # 创建 plan 主代理会话
    assert create_resp.status_code == 200  # 创建成功
    create_payload = create_resp.json()  # 解析响应
    assert create_payload["agent_id"] == "plan"  # 验证会话绑定到 plan 代理

    events = await collect_sse_events(  # 发送聊天请求并收集 SSE 事件
        chat_client,
        "/chat",
        json={
            "session_id": create_payload["session_id"],  # 使用 plan 会话
            "master_agent_name": "plan",  # 指定 plan 主代理
            "message": "请制定计划",  # 用户消息
        },
    )

    assert len(events) > 0  # 至少有一个事件
    assert events[0]["event"] == "run_started"  # 第一个事件是 run_started
    assert events[-1]["event"] == "run_completed"  # 最后一个事件是 run_completed


@pytest.mark.asyncio  # 标记为异步测试
async def test_chat_sse_rejects_master_agent_session_mismatch(chat_client):
    """验证 default 会话不能用 plan 主代理继续聊天。"""
    create_resp = await chat_client.post("/sessions")  # 创建默认主代理会话（不传 master_agent_name）
    session_id = create_resp.json()["session_id"]  # 获取会话 ID

    events = await collect_sse_events(  # 发送聊天请求并收集 SSE 事件
        chat_client,
        "/chat",
        json={
            "session_id": session_id,  # 使用默认会话
            "master_agent_name": "plan",  # 尝试使用 plan 主代理
            "message": "请制定计划",  # 用户消息
        },
    )

    assert len(events) == 1  # 只有一个错误事件
    assert events[0]["event"] == "request_failed"  # 事件类型为 request_failed
    assert events[0]["data"]["error_code"] == "MASTER_AGENT_SESSION_MISMATCH"  # 错误码为会话不匹配
