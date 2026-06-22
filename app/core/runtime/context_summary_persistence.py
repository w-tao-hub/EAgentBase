"""摘要持久化协作者。

负责把已经计算好的摘要计划写入正确的上下文消息流，
并保持主会话与 child 会话路径隔离。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.models.stored_message import StoredMessage

if TYPE_CHECKING:
    from app.core.ports.stores import SessionStore
    from app.core.runtime.context_builder import SummaryPersistenceTarget


@dataclass(slots=True)
class SummaryPersistencePlan:
    """摘要持久化计划。"""

    target: SummaryPersistenceTarget
    summary_message: StoredMessage
    active_start_message: StoredMessage | None
    active_start_offset: int | None


class SummaryPersistenceCoordinator:
    """根据摘要目标执行持久化。"""

    def __init__(self, session_store: "SessionStore") -> None:
        """保存会话存储依赖。"""
        self._session_store = session_store

    async def persist(self, plan: SummaryPersistencePlan) -> None:
        """把计划写入目标上下文。"""
        if plan.target.kind == "main":
            await self._session_store.append_main_context_summary(
                session_id=plan.target.session_id,
                message=plan.summary_message,
                active_start_message=plan.active_start_message,
                active_start_offset=plan.active_start_offset,
            )
            return
        if plan.target.kind == "child":
            if not plan.target.child_id:
                raise ValueError(
                    f"SummaryPersistenceTarget.kind='child' 但 child_id 为空，"
                    f"无法路由到正确的 child 上下文"
                )
            await self._session_store.append_child_context_summary(
                session_id=plan.target.session_id,
                child_id=plan.target.child_id,
                message=plan.summary_message,
                active_start_message=plan.active_start_message,
                active_start_offset=plan.active_start_offset,
            )
            return
        raise ValueError(
            f"SummaryPersistenceTarget.kind 非法: {plan.target.kind!r}，"
            f"仅支持 'main' 或 'child'"
        )
