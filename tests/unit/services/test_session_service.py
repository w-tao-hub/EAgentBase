"""SessionService 单元测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.services.session_service import SessionService, SessionView
from app.services.agent_provider import AgentProvider
from app.infra.store.redis_session_store import RedisSessionStore
from app.infra.store.redis_lock_store import RedisLockStore
from app.core.models.session import Session
from app.core.models.agent import Agent, AgentExecutionProfile, AgentPromptSource
from app.core.models.stored_message import StoredMessage


class FakeAgentProvider(AgentProvider):
    """模拟 Agent 提供者，支持多主代理（default 和 plan）。"""

    def __init__(self) -> None:  # 构造函数
        """初始化模拟提供者，注册 default 和 plan 两个主代理。"""
        self.default_agent = Agent(  # 创建默认 Agent
            agent_id="default",
            name="Default Master Agent",
            model="gpt-4.1-mini",
            system_prompt="你是一个乐于助人的助手。",
            temperature=0.2,
        )
        self.plan_agent = Agent(  # 创建 plan 主代理
            agent_id="plan",
            name="Plan Master Agent",
            model="gpt-4.1-mini",
            system_prompt="你是一个规划助手。",
            temperature=0.2,
        )
        # 按名称索引的主代理字典
        self._master_agents_by_name: dict[str, Agent] = {  # 名称到 Agent 映射
            "default": self.default_agent,
            "plan": self.plan_agent,
        }
        # 按 agent_id 索引的主代理字典
        self._agents_by_id: dict[str, Agent] = {  # agent_id 到 Agent 映射
            "default": self.default_agent,
            "plan": self.plan_agent,
        }

    def get_default(self) -> Agent:  # 获取默认 Agent
        """返回默认 Agent。"""
        return self.default_agent  # 返回默认主代理

    def get_sub_agents(self) -> list[Agent]:  # 获取子智能体
        """返回子智能体列表。"""
        return []  # 模拟无子智能体

    def get_master_profile_by_name(self, name: str) -> AgentExecutionProfile:  # 按名称获取主代理执行 profile
        """按主代理名称获取执行 profile。

        Args:
            name: 主代理名称。

        Returns:
            对应的 AgentExecutionProfile。

        Raises:
            ValueError: 当指定名称的主代理不存在时。
        """
        if name not in self._master_agents_by_name:  # 检查名称是否存在
            raise ValueError(f"未知的主代理名称: {name}")  # 抛出 ValueError
        agent = self._master_agents_by_name[name]  # 获取对应 Agent
        return AgentExecutionProfile(  # 构造最小化的执行 profile
            agent_id=agent.agent_id,
            agent=agent,
            prompt_source=AgentPromptSource(kind="file", path=""),
            runtime=object(),
            tool_registry=None,  # type: ignore[arg-type]
            tool_hook_pipeline=None,  # type: ignore[arg-type]
            max_turns=10,
        )

    def get_default_profile(self) -> AgentExecutionProfile:  # 获取默认执行 profile
        """获取系统默认的 Agent 执行 profile。"""
        return self.get_master_profile_by_name("default")  # 委托给按名称查询

    def get_profile(self, agent_id: str) -> AgentExecutionProfile:  # 按 agent_id 获取执行 profile
        """按 agent_id 获取对应的执行 profile。"""
        if agent_id not in self._agents_by_id:  # 检查 agent_id 是否存在
            raise ValueError(f"未知的 agent_id: {agent_id}")  # 抛出 ValueError
        return self.get_master_profile_by_name(agent_id)  # 委托给按名称查询

    def get_child_profile(self, subagent_type: str) -> AgentExecutionProfile:  # 按子代理类型获取执行 profile
        """按子代理类型名称获取对应的执行 profile。"""
        raise ValueError(f"未知的子代理类型: {subagent_type}")  # 模拟暂不支持子代理

    def get_master_profile(self, agent_id: str) -> AgentExecutionProfile:  # 按 agent_id 获取主代理执行 profile
        """按主代理 ID 获取执行 profile。"""
        return self.get_profile(agent_id)  # 委托给 get_profile


@pytest.fixture  # 定义 pytest 夹具
async def session_service(fake_redis):  # SessionService 夹具
    """提供配置好的 SessionService 实例。"""
    session_store = RedisSessionStore(fake_redis, key_prefix="test")  # 创建会话存储
    lock_store = RedisLockStore(fake_redis, key_prefix="test")  # 创建锁存储
    agent_provider = FakeAgentProvider()  # 创建模拟 Agent 提供者
    service = SessionService(  # 创建服务实例
        session_store=session_store,
        lock_store=lock_store,
        agent_provider=agent_provider,
    )
    return service  # 返回服务实例


@pytest.mark.asyncio  # 标记异步测试
async def test_create_session_returns_session_with_agent_id(session_service):  # 测试创建会话
    """测试创建会话应返回包含 agent_id 的会话对象。"""
    session = await session_service.create_session()  # 创建会话
    assert session.session_id is not None  # 验证 session_id 不为空
    assert len(session.session_id) > 0  # 验证 session_id 有长度
    assert session.agent_id == "default"  # 验证绑定了默认 Agent
    assert session.created_at is not None  # 验证创建时间不为空


@pytest.mark.asyncio  # 标记异步测试
async def test_get_session_returns_none_for_missing_session(session_service):  # 测试查询不存在的会话
    """测试查询不存在的会话应返回 None。"""
    result = await session_service.get_session("non-existent-id")  # 查询不存在的会话
    assert result is None  # 验证返回 None


@pytest.mark.asyncio  # 标记异步测试
async def test_get_session_view_returns_message_count_and_active_run_id(session_service, fake_redis):  # 测试获取会话视图
    """测试获取会话视图应返回消息数量和活跃 Run ID。"""
    # 创建会话
    session = await session_service.create_session()  # 创建会话
    session_id = session.session_id  # 获取会话 ID

    # 添加一些消息
    session_store = RedisSessionStore(fake_redis, key_prefix="test")  # 创建会话存储
    await session_store.append_message(  # 添加用户消息
        session_id,
        StoredMessage.create(role="user", content="Hello", timestamp=datetime.now(timezone.utc)),
    )
    await session_store.append_message(  # 添加助手消息
        session_id,
        StoredMessage.create(role="assistant", content="Hi there", timestamp=datetime.now(timezone.utc)),
    )

    # 获取活跃 run_id（通过加锁）
    lock_store = RedisLockStore(fake_redis, key_prefix="test")  # 创建锁存储
    await lock_store.acquire(session_id, "run-1", ttl_seconds=30)  # 获取锁

    # 获取会话视图
    view = await session_service.get_session_view(session_id)  # 获取会话视图
    assert view is not None  # 验证视图不为空
    assert view.session_id == session_id  # 验证会话 ID 正确
    assert view.agent_id == "default"  # 验证 Agent ID 正确
    assert view.message_count == 2  # 验证消息数量为 2
    assert view.active_run_id == "run-1"  # 验证活跃 Run ID 正确


@pytest.mark.asyncio  # 标记异步测试
async def test_get_session_view_returns_zero_count_for_no_messages(session_service):  # 测试空会话视图
    """测试没有消息的会话应返回消息数量为 0。"""
    session = await session_service.create_session()  # 创建会话
    session_id = session.session_id  # 获取会话 ID

    view = await session_service.get_session_view(session_id)  # 获取会话视图
    assert view is not None  # 验证视图不为空
    assert view.message_count == 0  # 验证消息数量为 0
    assert view.active_run_id is None  # 验证没有活跃 Run


@pytest.mark.asyncio  # 标记异步测试
async def test_create_session_can_bind_plan_master_agent(session_service):  # 测试绑定 plan 主代理
    """验证创建会话时可绑定 plan 主代理。"""
    session = await session_service.create_session(master_agent_name="plan")  # 创建 plan 会话

    assert session.agent_id == "plan"  # 验证绑定了 plan 主代理


@pytest.mark.asyncio  # 标记异步测试
async def test_create_session_defaults_to_default_master_agent(session_service):  # 测试默认使用 default 主代理
    """验证未指定主代理时仍兼容创建 default 会话。"""
    session = await session_service.create_session()  # 不指定 master_agent_name

    assert session.agent_id == "default"  # 验证绑定了 default 主代理
