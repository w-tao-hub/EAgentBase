"""SessionService 实现。

提供会话创建、查询和视图服务能力。
"""

from __future__ import annotations  # 启用未来注解

from dataclasses import dataclass  # 导入数据类装饰器
import asyncio  # 导入异步标准库，用于并行发起无依赖的 Redis 查询
from datetime import datetime, timezone  # 导入日期时间类和 UTC 时区
import logging  # 导入标准库日志模块，避免 services 依赖 infra 包路径
from typing import TYPE_CHECKING  # 导入类型检查标记
import uuid  # 导入 UUID 生成模块

from app.core.models.session import Session  # 导入 Session 模型

# 获取模块级日志器。
# 直接使用标准库 logging，保持 services 层不依赖 infra 包路径。
logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # 仅在类型检查时导入
    from app.infra.store.redis_session_store import RedisSessionStore  # 会话存储类型
    from app.infra.store.redis_lock_store import RedisLockStore  # 锁存储类型
    from app.services.agent_provider import AgentProvider  # Agent 提供者协议


@dataclass  # 定义数据类
class SessionView:
    """会话视图模型。

    用于向外部展示会话状态，包含消息数量和活跃 Run 信息。
    """

    # 会话唯一标识
    session_id: str

    # 绑定的 Agent 标识
    agent_id: str

    # 会话创建时间
    created_at: datetime

    # 当前会话中的消息数量
    message_count: int

    # 当前活跃的 Run ID，如果没有则为 None
    active_run_id: str | None


class SessionService:
    """会话服务。

    负责会话的生命周期管理和查询，包括：
    1. 创建新会话并绑定默认 Agent
    2. 查询会话元数据
    3. 获取会话完整视图（包含消息计数和活跃 Run）
    """

    def __init__(  # 构造函数
        self,
        session_store: RedisSessionStore,  # 会话存储实例
        lock_store: RedisLockStore,  # 锁存储实例
        agent_provider: AgentProvider,  # Agent 提供者实例
    ) -> None:
        """初始化 SessionService。

        Args:
            session_store: 用于持久化会话元数据的存储
            lock_store: 用于查询会话锁状态的存储
            agent_provider: 用于获取默认 Agent 配置
        """
        self._session_store = session_store  # 保存会话存储引用
        self._lock_store = lock_store  # 保存锁存储引用
        self._agent_provider = agent_provider  # 保存 Agent 提供者引用

    async def create_session(self) -> Session:  # 创建会话
        """创建新会话并绑定默认 Agent。

        Returns:
            新创建的 Session 实例
        """
        # 获取默认 Agent 配置
        agent = self._agent_provider.get_default()  # 从提供者获取默认 Agent

        # 构造 Session 实例
        session = Session(  # 创建新会话
            session_id=str(uuid.uuid4()),  # 生成 UUID 作为 session_id
            agent_id=agent.agent_id,  # 绑定默认 Agent ID
            created_at=datetime.now(timezone.utc),  # 使用当前 UTC 时间
        )

        # 持久化到 Redis
        await self._session_store.create_session(session)  # 存储会话元数据
        logger.info("会话创建成功: session_id=%s, agent_id=%s", session.session_id, session.agent_id)

        return session  # 返回创建的会话

    async def get_session(self, session_id: str) -> Session | None:  # 获取会话
        """查询会话元数据。

        Args:
            session_id: 会话唯一标识

        Returns:
            Session 实例，如果不存在则返回 None
        """
        return await self._session_store.get_session(session_id)  # 从存储查询

    async def get_session_view(self, session_id: str) -> SessionView | None:  # 获取会话视图
        """获取会话完整视图，包含消息数量和活跃 Run。

        Args:
            session_id: 会话唯一标识

        Returns:
            SessionView 实例，如果会话不存在则返回 None
        """
        # 获取会话元数据
        session = await self._session_store.get_session(session_id)  # 查询会话
        if session is None:  # 会话不存在
            return None  # 返回 None

        # message_count 与 active_run_id 查询相互独立，使用 gather 并行化以缩短连接持有时间
        message_count, active_run_id = await asyncio.gather(
            self._session_store.get_main_message_count(session_id),  # 查询主会话长期上下文消息计数
            self._lock_store.get_active_run_id(session_id),  # 查询活跃 Run
        )

        # 构造视图
        return SessionView(  # 返回会话视图
            session_id=session.session_id,  # 会话 ID
            agent_id=session.agent_id,  # Agent ID
            created_at=session.created_at,  # 创建时间
            message_count=message_count,  # 消息数量
            active_run_id=active_run_id,  # 活跃 Run ID
        )
