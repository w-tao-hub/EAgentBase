from __future__ import annotations

from dataclasses import dataclass
import asyncio
from datetime import datetime, timezone
import logging
from typing import TYPE_CHECKING
import uuid

from app.core.models.session import Session

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.infra.store.redis_session_store import RedisSessionStore
    from app.infra.store.redis_lock_store import RedisLockStore
    from app.services.agent_provider import AgentProvider


@dataclass
class SessionView:
    """会话视图模型。"""

    session_id: str
    agent_id: str
    created_at: datetime
    message_count: int
    active_run_id: str | None


class SessionService:
    """会话服务。"""

    def __init__(
        self,
        session_store: RedisSessionStore,
        lock_store: RedisLockStore,
        agent_provider: AgentProvider,
    ) -> None:
        self._session_store = session_store
        self._lock_store = lock_store
        self._agent_provider = agent_provider

    async def create_session(self) -> Session:
        """创建新会话并绑定默认 Agent。"""
        agent = self._agent_provider.get_default()

        session = Session(
            session_id=str(uuid.uuid4()),
            agent_id=agent.agent_id,
            created_at=datetime.now(timezone.utc),
        )

        await self._session_store.create_session(session)
        logger.info("会话创建成功: session_id=%s, agent_id=%s", session.session_id, session.agent_id)

        return session

    async def get_session(self, session_id: str) -> Session | None:
        """查询会话元数据。"""
        return await self._session_store.get_session(session_id)

    async def get_session_view(self, session_id: str) -> SessionView | None:
        """获取会话完整视图，包含消息数量和活跃 Run。"""
        session = await self._session_store.get_session(session_id)
        if session is None:
            return None

        # message_count 与 active_run_id 查询相互独立，使用 gather 并行化以缩短连接持有时间
        message_count, active_run_id = await asyncio.gather(
            self._session_store.get_main_message_count(session_id),
            self._lock_store.get_active_run_id(session_id),
        )

        return SessionView(
            session_id=session.session_id,
            agent_id=session.agent_id,
            created_at=session.created_at,
            message_count=message_count,
            active_run_id=active_run_id,
        )
