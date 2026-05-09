"""RedisSessionStore 实现。

提供会话元数据、主会话上下文与 child 上下文的 Redis 持久化存储。
本次仅面向新结构实现，不再兼容未上线时期的旧 key / 旧消息格式。
"""

from __future__ import annotations  # 启用未来注解

import asyncio  # 导入异步工具，用于并行发起无依赖的 Redis 读取。
from dataclasses import dataclass  # 导入数据类，用于表达上下文 key 组合与摘要边界。
from datetime import datetime, timezone  # 导入时间工具，用于 run 索引时间戳序列化。
import json  # 导入 JSON 序列化模块
from typing import TYPE_CHECKING, Any  # 导入类型检查标记与通用 pipeline 类型

from app.core.models.session import Session  # 导入 Session 模型
from app.core.models.stored_message import StoredMessage  # 导入新的直接存储消息模型，统一主会话与 child 上下文落库格式。
from app.infra.logging import get_logger  # 导入日志获取函数

# 获取模块级日志器
logger = get_logger(__name__)

if TYPE_CHECKING:  # 仅在类型检查时导入
    from redis.asyncio import Redis  # 异步 Redis 客户端类型


@dataclass(slots=True)
class ContextSummaryState:
    """会话最近一次上下文摘要的边界状态。"""

    summary_message_id: str  # 摘要消息的唯一标识(UUID)。
    active_start_message_id: str | None  # 当前活动窗口中，除摘要外首条保留消息的唯一标识；可能为None。
    summary_offset: int | None = None  # 摘要消息在 Redis List 中的绝对偏移；用于范围读取快路径。
    active_start_offset: int | None = None  # 活动窗口连续尾段的起始偏移；若无保留窗口则与摘要偏移相同。


@dataclass(frozen=True, slots=True)
class SessionChildSummary:
    """表示当前 session 下一个可恢复子代理的最新摘要。"""

    resume_id: str  # 子代理唯一标识
    subagent_type: str  # 子代理类型，如 Plan、Worker
    description: str  # 子代理的描述，用于 resume 时判断


@dataclass(slots=True, frozen=True)
class ContextKeySet:
    """同一条上下文消息流所需的 Redis key 组合。"""

    messages_key: str  # 消息 List key。
    dirty_key: str  # dirty 标记 key。
    summary_state_key: str  # 摘要边界状态 key。


class RedisSessionStore:
    """基于 Redis 的会话存储实现。

    新结构：
    - 会话元数据: {prefix}:session:{session_id}
    - 主会话上下文: {prefix}:session_main_messages:{session_id}
    - 主会话 dirty 标记: {prefix}:session_main_messages_dirty:{session_id}
    - 主会话摘要边界: {prefix}:session_main_context_summary_state:{session_id}
    - Session 关联 Run 索引: {prefix}:session_runs:{session_id}
    - Session 关联 Child 索引: {prefix}:session_children:{session_id}
    - Child 上下文: {prefix}:child_context_messages:{session_id}:{child_id}
    - Child dirty 标记: {prefix}:child_context_messages_dirty:{session_id}:{child_id}
    - Child 摘要边界: {prefix}:child_context_summary_state:{session_id}:{child_id}

    """

    def __init__(self, redis: Redis, key_prefix: str = "agent") -> None:  # 构造函数
        """初始化 SessionStore。

        Args:
            redis: Redis 异步客户端实例
            key_prefix: Redis key 前缀，用于命名空间隔离
        """
        self._redis = redis  # 保存 Redis 客户端引用
        self._key_prefix = key_prefix  # 保存 key 前缀

    def _session_key(self, session_id: str) -> str:  # 生成会话 key
        """生成会话元数据的 Redis key。"""
        return f"{self._key_prefix}:session:{session_id}"  # 拼接 key

    def _session_main_messages_key(self, session_id: str) -> str:
        """生成主会话上下文消息列表的 Redis key。"""
        return f"{self._key_prefix}:session_main_messages:{session_id}"

    def _session_main_messages_dirty_key(self, session_id: str) -> str:
        """生成主会话上下文 dirty 标记的 Redis key。"""
        return f"{self._key_prefix}:session_main_messages_dirty:{session_id}"

    def _session_main_context_summary_state_key(self, session_id: str) -> str:
        """生成主会话上下文摘要边界的 Redis key。"""
        return f"{self._key_prefix}:session_main_context_summary_state:{session_id}"

    def _session_runs_key(self, session_id: str) -> str:
        """生成 session 关联 run 索引的 Redis key。"""
        return f"{self._key_prefix}:session_runs:{session_id}"

    def _session_children_key(self, session_id: str) -> str:
        """生成 session 关联 child 摘要哈希的 Redis key。"""
        return f"{self._key_prefix}:session_children:{session_id}"

    def _child_context_messages_key(self, session_id: str, child_id: str) -> str:
        """生成 child 长期上下文消息列表的 Redis key。"""
        return f"{self._key_prefix}:child_context_messages:{session_id}:{child_id}"

    def _child_context_messages_dirty_key(self, session_id: str, child_id: str) -> str:
        """生成 child 长期上下文 dirty 标记的 Redis key。"""
        return f"{self._key_prefix}:child_context_messages_dirty:{session_id}:{child_id}"

    def _child_context_summary_state_key(self, session_id: str, child_id: str) -> str:
        """生成 child 长期上下文摘要边界的 Redis key。"""
        return f"{self._key_prefix}:child_context_summary_state:{session_id}:{child_id}"

    def _main_context_keys(self, session_id: str) -> ContextKeySet:
        """返回主会话上下文所需的 key 组合。"""
        return ContextKeySet(
            messages_key=self._session_main_messages_key(session_id),
            dirty_key=self._session_main_messages_dirty_key(session_id),
            summary_state_key=self._session_main_context_summary_state_key(session_id),
        )

    def _child_context_keys(self, session_id: str, child_id: str) -> ContextKeySet:
        """返回指定 child 长期上下文所需的 key 组合。"""
        return ContextKeySet(
            messages_key=self._child_context_messages_key(session_id, child_id),
            dirty_key=self._child_context_messages_dirty_key(session_id, child_id),
            summary_state_key=self._child_context_summary_state_key(session_id, child_id),
        )

    async def create_session(self, session: Session) -> Session:  # 创建会话
        """创建会话元数据。

        Args:
            session: 要创建的 Session 实例

        Returns:
            创建的 Session 实例
        """
        session_key = self._session_key(session.session_id)  # 获取会话 key
        session_data = {  # 构造会话数据字典
            "session_id": session.session_id,  # 会话 ID
            "agent_id": session.agent_id,  # Agent ID
            "created_at": session.created_at.isoformat(),  # ISO 格式时间
        }
        await self._redis.hset(session_key, mapping=session_data)  # 存储到 Redis
        return session  # 返回创建的会话

    async def get_session(self, session_id: str) -> Session | None:  # 获取会话
        """读取会话元数据。

        Args:
            session_id: 会话唯一标识

        Returns:
            Session 实例，如果不存在则返回 None
        """
        session_key = self._session_key(session_id)  # 获取会话 key
        data = await self._redis.hgetall(session_key)  # 从 Redis 读取
        if not data:  # 如果数据为空
            logger.debug("会话不存在: session_id=%s", session_id)
            return None  # 返回 None 表示不存在
        logger.debug("会话查询成功: session_id=%s", session_id)
        return Session(  # 构造 Session 实例
            session_id=data["session_id"],  # 会话 ID
            agent_id=data["agent_id"],  # Agent ID
            created_at=datetime.fromisoformat(data["created_at"]),  # 解析 ISO 时间
        )

    async def add_session_run(
        self,
        session_id: str,
        run_id: str,
        created_at: datetime | None = None,
        created_at_ts: float | None = None,
    ) -> None:
        """向 session 的 run 索引中登记一条 run_id。"""
        if created_at_ts is not None:  # 调用方若已直接给出时间戳，则优先复用，避免重复 datetime 转换。
            score = created_at_ts
        else:
            score = (created_at or datetime.now(timezone.utc)).timestamp()  # 用创建时间做排序分值，便于后续按时间回放。
        await self._redis.zadd(self._session_runs_key(session_id), {run_id: score})  # 使用 ZSET 保留时间顺序索引。

    def queue_add_session_run(
        self,
        pipeline: Any,
        session_id: str,
        run_id: str,
        created_at: datetime | None = None,
        created_at_ts: float | None = None,
    ) -> None:
        """向 pipeline 中排入一条 session_run 索引登记命令。"""
        if created_at_ts is not None:
            score = created_at_ts
        else:
            score = (created_at or datetime.now(timezone.utc)).timestamp()
        pipeline.zadd(self._session_runs_key(session_id), {run_id: score})

    async def list_session_runs(self, session_id: str) -> list[str]:
        """列出某个 session 关联的全部 run_id。"""
        return await self._redis.zrange(self._session_runs_key(session_id), 0, -1)  # 保持 Redis 默认升序返回，便于调用方按时间正序消费。

    async def list_session_run_ids(self, session_id: str) -> list[str]:
        """按更直白的命名返回某个 session 关联的全部 run_id。"""
        return await self.list_session_runs(session_id)  # 提供别名，方便调用方逐步迁移。

    async def ensure_session_child_registered(self, session_id: str, child_id: str) -> None:
        """确保 session_children 中存在当前 child 的记录，但不破坏已有摘要。

        使用 HSETNX 原子写入，避免 TOCTOU 竞态条件。
        """
        key = self._session_children_key(session_id)
        placeholder = SessionChildSummary(
            resume_id=child_id,
            subagent_type="",
            description="",
        )
        await self._redis.hsetnx(key, child_id, self._serialize_session_child_summary(placeholder))

    def queue_upsert_session_child_summary(
        self,
        pipeline: Any,
        session_id: str,
        child_id: str,
        subagent_type: str,
        description: str,
    ) -> None:
        """向 pipeline 中排入一条 child 摘要覆盖写命令。"""
        summary = SessionChildSummary(
            resume_id=child_id,
            subagent_type=subagent_type,
            description=description,
        )
        pipeline.hset(
            self._session_children_key(session_id),
            child_id,
            self._serialize_session_child_summary(summary),
        )

    async def list_session_children(self, session_id: str) -> list[str]:
        """列出某个 session 关联的全部 child_id。"""
        child_ids = await self._redis.hkeys(self._session_children_key(session_id))  # 从 HASH 中读取全部 field 作为 child_id 列表。
        return sorted(child_ids)  # 统一按字典序返回，保证接口输出稳定。

    async def list_session_child_ids(self, session_id: str) -> list[str]:
        """按更直白的命名返回某个 session 关联的全部 child_id。"""
        return await self.list_session_children(session_id)  # 提供别名，方便调用方逐步迁移。

    async def upsert_session_child_summary(
        self,
        session_id: str,
        child_id: str,
        subagent_type: str,
        description: str,
    ) -> None:
        """写入或覆盖 child 摘要，供首次派发和 resume 共用。"""
        summary = SessionChildSummary(
            resume_id=child_id,
            subagent_type=subagent_type,
            description=description,
        )
        await self._redis.hset(
            self._session_children_key(session_id),
            child_id,
            self._serialize_session_child_summary(summary),
        )

    async def list_session_child_summaries(self, session_id: str) -> list[SessionChildSummary]:
        """读取当前 session 下全部 child 摘要，并按 child_id 排序返回。"""
        payload = await self._redis.hgetall(self._session_children_key(session_id))
        summaries: list[SessionChildSummary] = []
        for child_id in sorted(payload):
            summaries.append(self._deserialize_session_child_summary(payload[child_id], fallback_child_id=child_id))
        return summaries

    @staticmethod
    def _serialize_session_child_summary(summary: SessionChildSummary) -> str:
        """把 child 摘要序列化为 JSON 字符串。"""
        return json.dumps(
            {
                "resume_id": summary.resume_id,
                "subagent_type": summary.subagent_type,
                "description": summary.description,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _deserialize_session_child_summary(payload: str, *, fallback_child_id: str) -> SessionChildSummary:
        """把 Redis 中的 JSON 字符串反序列化为 child 摘要。"""
        data = json.loads(payload)
        return SessionChildSummary(
            resume_id=str(data.get("resume_id") or fallback_child_id),
            subagent_type=str(data.get("subagent_type") or ""),
            description=str(data.get("description") or ""),
        )

    async def get_main_message_count(self, session_id: str) -> int:
        """获取主会话上下文中的消息数量。"""
        return await self._get_message_count_from_keys(self._main_context_keys(session_id))  # 主会话上下文只读新结构。

    async def append_main_message(
        self,
        session_id: str,
        message: StoredMessage,
        source_run_id: str | None = None,
        child_id: str | None = None,
    ) -> None:
        """向主会话上下文追加一条消息。"""
        await self._append_message_to_keys(
            self._main_context_keys(session_id),
            message,
            source_run_id=source_run_id,
            child_id=child_id,
        )  # 直接写入新主会话上下文，并记录来源 run。

    def queue_append_main_message(
        self,
        pipeline: Any,
        session_id: str,
        message: StoredMessage,
        source_run_id: str | None = None,
        child_id: str | None = None,
    ) -> None:
        """向主会话上下文的 Redis pipeline 中排入一条消息追加命令。"""
        self._queue_append_message_to_keys(
            pipeline,
            self._main_context_keys(session_id),
            message,
            source_run_id=source_run_id,
            child_id=child_id,
        )  # 用 pipeline 合并写入，减少终态双写等场景的 Redis 往返。

    async def list_main_messages(self, session_id: str, start: int = 0, end: int = -1) -> list[StoredMessage]:
        """读取主会话上下文消息列表。"""
        return await self._list_messages_from_keys(
            self._main_context_keys(session_id),
            start=start,
            end=end,
        )  # 主会话上下文读取直接走新 key。

    async def get_main_context_summary_state(self, session_id: str) -> ContextSummaryState | None:
        """读取主会话上下文最近一次摘要边界。"""
        return await self._get_context_summary_state_from_keys(self._main_context_keys(session_id))  # 主会话摘要边界直接读新 key。

    async def append_main_context_summary(
        self,
        session_id: str,
        message: StoredMessage,
        active_start_message: StoredMessage | None,
        active_start_offset: int | None = None,
    ) -> ContextSummaryState:
        """向主会话上下文追加摘要消息，并刷新摘要边界。"""
        return await self._append_context_summary_to_keys(
            self._main_context_keys(session_id),
            message,
            active_start_message=active_start_message,
            active_start_offset=active_start_offset,
            source_run_id=None,
            child_id=None,
        )

    async def list_main_active_messages(self, session_id: str) -> list[StoredMessage]:
        """读取主会话当前会参与上下文构建的活动窗口。"""
        return await self._list_active_messages_from_keys(self._main_context_keys(session_id))  # 主会话活动窗口直接走新结构。

    async def list_active_main_messages(self, session_id: str) -> list[StoredMessage]:
        """按当前服务层使用的命名读取主会话活动窗口。"""
        return await self.list_main_active_messages(session_id)

    async def list_main_active_messages_with_indices(self, session_id: str) -> tuple[list[StoredMessage], list[int]]:
        """读取主会话活动窗口及其绝对偏移映射。"""
        return await self._list_active_messages_with_indices_from_keys(self._main_context_keys(session_id))  # 主会话活动窗口直接走新结构。

    async def list_active_main_messages_with_indices(self, session_id: str) -> tuple[list[StoredMessage], list[int]]:
        """按当前服务层使用的命名读取主会话活动窗口及其绝对偏移映射。"""
        return await self.list_main_active_messages_with_indices(session_id)

    async def mark_main_history_dirty(self, session_id: str) -> None:
        """标记主会话上下文存在待修复历史。"""
        await self._mark_history_dirty_by_keys(self._main_context_keys(session_id))  # 统一写入主会话 dirty key。

    async def is_main_history_dirty(self, session_id: str) -> bool:
        """查询主会话上下文是否已被标记为 dirty。"""
        return await self._is_history_dirty_by_keys(self._main_context_keys(session_id))  # 主会话 dirty 直接读新 key。

    async def get_child_message_count(self, session_id: str, child_id: str) -> int:
        """获取指定 child 长期上下文中的消息数量。"""
        return await self._get_message_count_from_keys(self._child_context_keys(session_id, child_id))  # child 不存在旧 key 兼容问题，直接读新结构。

    async def append_child_message(
        self,
        session_id: str,
        child_id: str,
        message: StoredMessage,
        source_run_id: str | None = None,
        subagent_type: str | None = None,
    ) -> None:
        """向指定 child 长期上下文追加一条消息。

        subagent_type 用于在 child 上下文消息中记录当前子代理的类型，
        后续 resume 校验时通过该字段判断 child_id 与 subagent_type 的一致性。
        """
        await self.ensure_session_child_registered(session_id, child_id)  # 只要某个 child 产生了上下文，就要确保它被登记到 session_children 索引。
        await self._append_message_to_keys(
            self._child_context_keys(session_id, child_id),
            message,
            source_run_id=source_run_id,
            child_id=child_id,
            subagent_type=subagent_type,
        )  # 再把消息落到 child 自己的长期上下文流。

    def queue_append_child_message(
        self,
        pipeline: Any,
        session_id: str,
        child_id: str,
        message: StoredMessage,
        source_run_id: str | None = None,
        subagent_type: str | None = None,
    ) -> None:
        """向指定 child 长期上下文的 Redis pipeline 中排入一条消息追加命令。"""
        self._queue_append_message_to_keys(
            pipeline,
            self._child_context_keys(session_id, child_id),
            message,
            source_run_id=source_run_id,
            child_id=child_id,
            subagent_type=subagent_type,
        )  # 仅排入 child 上下文写命令；索引登记需由调用方单独保证。

    async def list_child_messages(
        self,
        session_id: str,
        child_id: str,
        start: int = 0,
        end: int = -1,
    ) -> list[StoredMessage]:
        """读取指定 child 长期上下文消息列表。"""
        return await self._list_messages_from_keys(self._child_context_keys(session_id, child_id), start=start, end=end)  # child 直接读取自己的上下文消息流。

    async def list_child_context_messages(
        self,
        session_id: str,
        child_id: str,
        start: int = 0,
        end: int = -1,
    ) -> list[StoredMessage]:
        """按更直白的命名读取指定 child 的长期上下文消息列表。"""
        return await self.list_child_messages(session_id, child_id, start=start, end=end)

    async def get_child_context_summary_state(self, session_id: str, child_id: str) -> ContextSummaryState | None:
        """读取指定 child 长期上下文最近一次摘要边界。"""
        return await self._get_context_summary_state_from_keys(self._child_context_keys(session_id, child_id))  # child 不需要兼容旧 key，直接读新摘要状态。

    async def append_child_context_summary(
        self,
        session_id: str,
        child_id: str,
        message: StoredMessage,
        active_start_message: StoredMessage | None,
        active_start_offset: int | None = None,
    ) -> ContextSummaryState:
        """向指定 child 长期上下文追加摘要消息，并刷新摘要边界。"""
        await self.ensure_session_child_registered(session_id, child_id)  # child 摘要一旦存在，该 child 也必须可被 session_children 找到。
        return await self._append_context_summary_to_keys(
            self._child_context_keys(session_id, child_id),
            message,
            active_start_message=active_start_message,
            active_start_offset=active_start_offset,
            source_run_id=None,
            child_id=child_id,
        )

    async def list_child_active_messages(self, session_id: str, child_id: str) -> list[StoredMessage]:
        """读取指定 child 长期上下文的活动窗口。"""
        return await self._list_active_messages_from_keys(self._child_context_keys(session_id, child_id))  # child 直接复用通用活动窗口恢复逻辑。

    async def list_child_active_messages_with_indices(
        self,
        session_id: str,
        child_id: str,
    ) -> tuple[list[StoredMessage], list[int]]:
        """读取指定 child 长期上下文活动窗口及其绝对偏移映射。"""
        return await self._list_active_messages_with_indices_from_keys(self._child_context_keys(session_id, child_id))  # child 直接读取自己的消息流与摘要边界。

    async def mark_child_history_dirty(self, session_id: str, child_id: str) -> None:
        """标记指定 child 长期上下文存在待修复历史。"""
        await self.ensure_session_child_registered(session_id, child_id)  # child 被打脏后，后续修复流程也必须能先枚举到这个 child。
        await self._mark_history_dirty_by_keys(self._child_context_keys(session_id, child_id))  # 统一写入 child dirty key。

    async def is_child_history_dirty(self, session_id: str, child_id: str) -> bool:
        """查询指定 child 长期上下文是否已被标记为 dirty。"""
        return await self._is_history_dirty_by_keys(self._child_context_keys(session_id, child_id))  # child 没有旧 key 兼容要求，直接读新 dirty key。

    async def get_message_count(self, session_id: str) -> int:  # 获取消息数量
        """兼容旧单流 API：获取主会话上下文的消息数量。"""
        return await self.get_main_message_count(session_id)  # 旧调用点默认等价于主会话上下文计数。

    async def append_message(
        self,
        session_id: str,
        message: StoredMessage,
        source_run_id: str | None = None,
    ) -> None:  # 追加消息
        """兼容旧单流 API：向主会话上下文追加一条消息。"""
        await self.append_main_message(session_id, message, source_run_id=source_run_id)  # 旧调用点默认落到新的主会话上下文。

    def queue_append_message(
        self,
        pipeline: Any,
        session_id: str,
        message: StoredMessage,
        source_run_id: str | None = None,
    ) -> None:
        """兼容旧单流 API：向主会话上下文 pipeline 中排入一条消息追加命令。"""
        self.queue_append_main_message(
            pipeline,
            session_id,
            message,
            source_run_id=source_run_id,
        )  # 旧 pipeline 追加也统一转发到主会话上下文。

    async def list_messages(self, session_id: str, start: int = 0, end: int = -1) -> list[StoredMessage]:  # 列出消息
        """兼容旧单流 API：读取主会话上下文消息列表。"""
        return await self.list_main_messages(session_id, start=start, end=end)  # 旧读取默认等价于读主会话上下文。

    async def get_context_summary_state(self, session_id: str) -> ContextSummaryState | None:
        """兼容旧单流 API：读取主会话上下文最近一次摘要边界。"""
        return await self.get_main_context_summary_state(session_id)  # 旧调用点默认等价于主会话摘要边界。

    async def append_context_summary(
        self,
        session_id: str,
        message: StoredMessage,
        active_start_message: StoredMessage | None,
        active_start_offset: int | None = None,
    ) -> ContextSummaryState:
        """兼容旧单流 API：向主会话上下文追加摘要消息，并刷新摘要边界。"""
        return await self.append_main_context_summary(
            session_id,
            message,
            active_start_message=active_start_message,
            active_start_offset=active_start_offset,
        )

    async def list_active_messages(self, session_id: str) -> list[StoredMessage]:
        """兼容旧单流 API：读取主会话当前活动窗口。"""
        return await self.list_main_active_messages(session_id)  # 旧调用点默认等价于主会话活动窗口。

    async def list_active_messages_with_indices(self, session_id: str) -> tuple[list[StoredMessage], list[int]]:
        """兼容旧单流 API：读取主会话活动窗口及其绝对偏移映射。"""
        return await self.list_main_active_messages_with_indices(session_id)  # 旧调用点默认等价于主会话活动窗口查询。

    async def mark_history_dirty(self, session_id: str) -> None:  # 标记历史存在错乱
        """兼容旧单流 API：标记主会话上下文存在待修复历史。"""
        await self.mark_main_history_dirty(session_id)  # 旧打脏行为统一映射到主会话 dirty 标记。

    async def is_history_dirty(self, session_id: str) -> bool:  # 查询历史 dirty 标记
        """兼容旧单流 API：查询主会话上下文是否已被标记为 dirty。"""
        return await self.is_main_history_dirty(session_id)  # 旧查询行为统一映射到主会话 dirty 标记。

    @staticmethod
    def _serialize_message(
        message: StoredMessage,
        *,
        source_run_id: str | None = None,
        child_id: str | None = None,
        subagent_type: str | None = None,
    ) -> str:
        """把 StoredMessage 序列化为 Redis 中的 JSON 文本。

        主会话仅允许保留 child_id，不保留 subagent_type；child 上下文可同时保留两者。
        """
        if child_id is None:  # 主上下文无 child 关联时，显式清空 child 元数据
            message = message.model_copy(
                update={
                    "meta": message.meta.model_copy(
                        update={"child_id": None, "subagent_type": None}
                    )
                },
                deep=True,
            )
        elif subagent_type is None:  # 主会话有关联 child 时，仅保留 child_id，不保留 subagent_type
            message = message.model_copy(
                update={
                    "meta": message.meta.model_copy(
                        update={"subagent_type": None}
                    )
                },
                deep=True,
            )
        stored_message = message.with_meta_overrides(
            source_run_id=source_run_id,
            child_id=child_id,
            subagent_type=subagent_type,
        )  # 落库前统一补齐本次写入的来源元数据，避免额外消息模型转换。
        return json.dumps(stored_message.to_storage_dict(), ensure_ascii=False)

    async def delete_session_main_context(self, session_id: str) -> int:
        """删除主会话上下文及其摘要/dirty 状态。"""
        return int(
            await self._redis.delete(
                self._session_main_messages_key(session_id),
                self._session_main_messages_dirty_key(session_id),
                self._session_main_context_summary_state_key(session_id),
            )
        )

    async def delete_child_context(self, session_id: str, child_id: str) -> int:
        """删除单个 child 长期上下文及其摘要/dirty 状态。"""
        return int(
            await self._redis.delete(
                self._child_context_messages_key(session_id, child_id),
                self._child_context_messages_dirty_key(session_id, child_id),
                self._child_context_summary_state_key(session_id, child_id),
            )
        )

    async def delete_session_metadata_and_indices(self, session_id: str) -> int:
        """删除 session 元数据及 run/child 索引。"""
        return int(
            await self._redis.delete(
                self._session_key(session_id),
                self._session_runs_key(session_id),
                self._session_children_key(session_id),
            )
        )

    @staticmethod
    def _deserialize_message(message_json: str) -> StoredMessage:
        """把 Redis 中的 JSON 文本反序列化为 StoredMessage。"""
        return StoredMessage.from_storage_dict(json.loads(message_json))  # 读取时直接恢复唯一消息模型。

    async def _get_message_count_from_keys(self, context_keys: ContextKeySet) -> int:
        """读取指定上下文消息数量。"""
        return await self._redis.llen(context_keys.messages_key)  # 直接读取目标消息列表长度。

    async def _append_message_to_keys(
        self,
        context_keys: ContextKeySet,
        message: StoredMessage,
        *,
        source_run_id: str | None = None,
        child_id: str | None = None,
        subagent_type: str | None = None,
    ) -> None:
        """向指定上下文追加一条消息。"""
        await self._redis.rpush(
            context_keys.messages_key,
            self._serialize_message(message, source_run_id=source_run_id, child_id=child_id, subagent_type=subagent_type),
        )  # 统一把消息追加到目标上下文尾部，保持时间顺序不变。

    def _queue_append_message_to_keys(
        self,
        pipeline: Any,
        context_keys: ContextKeySet,
        message: StoredMessage,
        *,
        source_run_id: str | None = None,
        child_id: str | None = None,
        subagent_type: str | None = None,
    ) -> None:
        """向指定上下文的 Redis pipeline 中排入一条消息追加命令。"""
        pipeline.rpush(
            context_keys.messages_key,
            self._serialize_message(message, source_run_id=source_run_id, child_id=child_id, subagent_type=subagent_type),
        )  # 只把命令排入 pipeline，真正的网络往返由调用方统一 execute。

    async def _list_messages_from_keys(
        self,
        context_keys: ContextKeySet,
        start: int = 0,
        end: int = -1,
    ) -> list[StoredMessage]:
        """读取指定上下文消息列表。"""
        message_jsons = await self._redis.lrange(context_keys.messages_key, start, end)  # 使用 LRANGE 读取目标上下文指定片段。
        return [self._deserialize_message(message_json) for message_json in message_jsons]  # 逐条恢复为 StoredMessage，供上层直接复用。

    async def _find_message_offset_by_id(self, messages_key: str, message_id: str) -> int | None:
        """按 message_id 扫描定位消息在 Redis List 中的绝对偏移。

        新存储格式会把 `_meta` 一并写入 JSON，调用方又只持有反序列化后的 StoredMessage，
        因此这里不再依赖旧的“完整 JSON 文本精确匹配”，而是退化为一次顺序扫描。
        该路径只在调用方未显式传 `active_start_offset` 时启用。
        """
        message_jsons = await self._redis.lrange(messages_key, 0, -1)  # 直接读取完整上下文，按 message_id 顺序扫描定位。
        for index, message_json in enumerate(message_jsons):
            if self._deserialize_message(message_json).message_id == message_id:
                return index
        return None

    async def _get_context_summary_state_from_keys(self, context_keys: ContextKeySet) -> ContextSummaryState | None:
        """读取指定上下文最近一次摘要边界。"""
        raw_value = await self._redis.get(context_keys.summary_state_key)  # 读取原始 JSON 文本。
        if not raw_value:  # 从未写入摘要边界时直接返回 None。
            return None
        return self._deserialize_context_summary_state(raw_value)  # 再把 JSON 文本恢复为统一的摘要状态载体。

    async def _append_context_summary_to_keys(
        self,
        context_keys: ContextKeySet,
        message: StoredMessage,
        active_start_message: StoredMessage | None,
        active_start_offset: int | None = None,
        source_run_id: str | None = None,
        child_id: str | None = None,
    ) -> ContextSummaryState:
        """向指定上下文追加摘要消息，并刷新最近一次摘要边界。"""
        summary_offset = await self._redis.llen(context_keys.messages_key)  # 在写入摘要前先拿当前长度，新摘要写入后其偏移就是旧长度。
        message_json = self._serialize_message(
            message,
            source_run_id=source_run_id,
            child_id=child_id,
        )  # 先把摘要消息转成 JSON 文本，便于后续复用。
        resolved_active_start_offset = active_start_offset  # 优先使用调用方直接传入的绝对偏移，避免重复扫描 Redis。
        if resolved_active_start_offset is None and active_start_message is not None:  # 未显式传偏移时，退化为按 message_id 扫描定位。
            resolved_active_start_offset = await self._find_message_offset_by_id(
                context_keys.messages_key,
                active_start_message.message_id,
            )  # 新存储格式会附带 _meta，不能再靠旧 JSON 文本做精确 LPOS 匹配。
        if resolved_active_start_offset is None:  # 没有保留窗口时，连续尾段应从摘要消息自身开始读取。
            resolved_active_start_offset = summary_offset

        summary_state = ContextSummaryState(
            summary_message_id=message.message_id,  # 记录摘要消息的 UUID。
            active_start_message_id=(
                active_start_message.message_id if active_start_message is not None else None
            ),  # 记录活动窗口起点消息的 UUID。
            summary_offset=summary_offset,  # 记录摘要消息在 Redis List 中的绝对偏移。
            active_start_offset=resolved_active_start_offset,  # 记录活动窗口尾段起点偏移，供后续范围读取快路径复用。
        )
        summary_state_json = self._serialize_context_summary_state(summary_state)  # 先把摘要边界序列化，避免在 pipeline 中重复构造数据。

        pipeline = self._redis.pipeline()  # 将摘要消息落库与边界刷新合并到一次 Redis 往返。
        pipeline.rpush(context_keys.messages_key, message_json)  # 先排入摘要消息追加命令，保持历史尾部写入语义不变。
        pipeline.set(context_keys.summary_state_key, summary_state_json)  # 再排入摘要边界状态刷新命令，避免额外一次独立 await。
        await pipeline.execute()  # 统一执行两条无数据依赖的写命令，缩小短暂不一致窗口。
        return summary_state  # 返回边界状态，便于调用方测试和调试。

    async def _list_active_messages_from_keys(self, context_keys: ContextKeySet) -> list[StoredMessage]:
        """读取指定上下文当前会参与构建的活动窗口。"""
        messages, summary_state = await asyncio.gather(
            self._list_messages_from_keys(context_keys),  # 并行读取完整历史，便于普通调用方直接复用。
            self._get_context_summary_state_from_keys(context_keys),  # 同时读取最近一次摘要边界状态。
        )
        active_messages, _active_indices = self.build_active_messages_with_indices(
            messages,
            summary_state,
        )  # 直接基于已拿到的历史与边界重建活动窗口，避免重复 Redis 读取。
        return active_messages  # 只返回活动消息列表，供普通调用方直接使用。

    async def _list_active_messages_with_indices_from_keys(
        self,
        context_keys: ContextKeySet,
    ) -> tuple[list[StoredMessage], list[int]]:
        """读取指定上下文当前活动窗口，并返回其在 Redis List 中的绝对偏移映射。"""
        summary_state = await self._get_context_summary_state_from_keys(context_keys)  # 先读取最近一次摘要边界。
        if summary_state is None:  # 未启用摘要压缩时，活动窗口就是完整历史。
            messages = await self._list_messages_from_keys(context_keys)  # 直接全量读取当前历史。
            return messages, list(range(len(messages)))  # 此时绝对偏移就是当前列表索引。

        start_offset = summary_state.active_start_offset  # 优先使用状态中缓存的活动窗口起点偏移。
        if start_offset is None:  # 旧状态未记录活动窗口偏移时，退化为从摘要位置开始读取。
            start_offset = summary_state.summary_offset
        if (
            start_offset is not None
            and summary_state.summary_offset is not None
            and start_offset <= summary_state.summary_offset
        ):  # 只有起点仍在摘要之前或等于摘要时，才可走范围读取快路径。
            ranged_messages = await self._list_messages_from_keys(context_keys, start=start_offset, end=-1)  # 只读取活动窗口连续尾段，避免再次全量 LRANGE。
            rebuilt_window = self._rebuild_active_window_from_ranged_messages(
                ranged_messages=ranged_messages,
                summary_state=summary_state,
                start_offset=start_offset,
            )
            if rebuilt_window is not None:  # 快路径命中且校验通过时，直接返回重建后的活动窗口。
                return rebuilt_window

        messages = await self._list_messages_from_keys(context_keys)  # 快路径失效时回退到完整历史，保证语义正确优先。
        return self.build_active_messages_with_indices(messages, summary_state)  # 继续沿用按 UUID 扫描的稳妥路径。

    @staticmethod
    def _serialize_context_summary_state(summary_state: ContextSummaryState) -> str:
        """把摘要边界状态序列化为 Redis JSON 文本。"""
        return json.dumps(
            {
                "summary_message_id": summary_state.summary_message_id,
                "active_start_message_id": summary_state.active_start_message_id,
                "summary_offset": summary_state.summary_offset,
                "active_start_offset": summary_state.active_start_offset,
            }
        )

    @staticmethod
    def _deserialize_context_summary_state(raw_value: str) -> ContextSummaryState:
        """把 Redis 中的摘要状态 JSON 文本反序列化为 ContextSummaryState。"""
        data = json.loads(raw_value)  # 解析 JSON，恢复消息唯一标识与偏移数据。
        return ContextSummaryState(
            summary_message_id=data["summary_message_id"],  # 恢复摘要消息 UUID。
            active_start_message_id=data.get("active_start_message_id") or None,  # 恢复活动窗口起点 UUID。
            summary_offset=data.get("summary_offset"),  # 恢复摘要消息偏移；旧状态缺失时保持 None。
            active_start_offset=data.get("active_start_offset"),  # 恢复活动窗口起点偏移；旧状态缺失时保持 None。
        )

    async def _mark_history_dirty_by_keys(self, context_keys: ContextKeySet) -> None:
        """标记指定上下文历史存在待修复问题。"""
        await self._redis.set(context_keys.dirty_key, "1")  # 写入固定标记值，表示该上下文历史需要后续排查。

    async def _is_history_dirty_by_keys(self, context_keys: ContextKeySet) -> bool:
        """查询指定上下文历史是否已被标记为 dirty。"""
        value = await self._redis.get(context_keys.dirty_key)  # 读取 dirty 标记值。
        return value == "1"  # 只有固定值 1 时才视为已打脏标记。

    @staticmethod
    def build_active_messages(
        messages: list[StoredMessage],
        summary_state: ContextSummaryState | None,
    ) -> list[StoredMessage]:
        """根据最近一次摘要边界重建活动消息窗口。"""
        active_messages, _active_indices = RedisSessionStore.build_active_messages_with_indices(messages, summary_state)  # 复用统一的重建逻辑，避免两套实现漂移。
        return active_messages  # 对外仅暴露消息列表，保持既有调用方不变。

    @staticmethod
    def build_active_messages_with_indices(
        messages: list[StoredMessage],
        summary_state: ContextSummaryState | None,
    ) -> tuple[list[StoredMessage], list[int]]:
        """根据最近一次摘要边界重建活动消息窗口，并保留索引映射。"""
        if summary_state is None:  # 从未压缩过时，完整历史就是活动窗口。
            return list(messages), list(range(len(messages)))

        summary_index: int | None = None  # 按 UUID 扫描定位摘要消息位置。
        active_start_index: int | None = None  # 按 UUID 扫描定位活动窗口起点位置。
        for index, message in enumerate(messages):
            if message.message_id == summary_state.summary_message_id:
                summary_index = index
            if summary_state.active_start_message_id is not None and message.message_id == summary_state.active_start_message_id:
                active_start_index = index

        if summary_index is None:  # 摘要消息未找到时，直接回退到完整历史。
            return list(messages), list(range(len(messages)))

        if active_start_index is None:  # 起点未找到，则活动窗口仅包含摘要消息及其之后的所有消息。
            active_messages: list[StoredMessage] = [messages[summary_index]]
            active_messages.extend(messages[summary_index + 1 :])
            active_indices: list[int] = [summary_index]
            active_indices.extend(range(summary_index + 1, len(messages)))
            return active_messages, active_indices

        if active_start_index < 0 or active_start_index > summary_index:  # 起点位置非法时回退完整历史。
            return list(messages), list(range(len(messages)))

        active_messages = [messages[summary_index]]  # 首位固定放最近一次摘要消息。
        active_messages.extend(messages[active_start_index:summary_index])  # 接上摘要前仍需保留的最近历史。
        active_messages.extend(messages[summary_index + 1 :])  # 最后接上摘要之后产生的所有新消息。
        active_indices = [summary_index]  # 首位索引固定对应摘要消息在完整历史中的位置。
        active_indices.extend(range(active_start_index, summary_index))  # 紧随其后的是摘要前保留窗口。
        active_indices.extend(range(summary_index + 1, len(messages)))  # 最后追加摘要之后产生的所有新消息。
        return active_messages, active_indices

    @staticmethod
    def _rebuild_active_window_from_ranged_messages(
        ranged_messages: list[StoredMessage],
        summary_state: ContextSummaryState,
        start_offset: int,
    ) -> tuple[list[StoredMessage], list[int]] | None:
        """根据连续尾段范围读取结果，重建“摘要优先”的活动窗口。"""
        if summary_state.summary_offset is None:  # 缺少摘要偏移时无法使用范围读取快路径。
            return None
        if start_offset > summary_state.summary_offset:  # 起点跑到摘要之后时，当前状态已不可信。
            return None
        summary_relative_index = summary_state.summary_offset - start_offset  # 计算摘要消息在尾段窗口中的相对位置。
        if summary_relative_index < 0 or summary_relative_index >= len(ranged_messages):  # 摘要不在读取结果中时，说明偏移已失效。
            return None
        summary_message = ranged_messages[summary_relative_index]  # 取出理论上的摘要消息，用于做 ID 校验。
        if summary_message.message_id != summary_state.summary_message_id:  # 摘要位置的消息 UUID 不匹配时，必须回退到完整扫描。
            return None
        if (
            summary_state.active_start_message_id is not None
            and start_offset < summary_state.summary_offset
            and ranged_messages
            and ranged_messages[0].message_id != summary_state.active_start_message_id
        ):  # 活动窗口起点 UUID 不匹配时，说明前缀被裁剪或状态已漂移，不能信任快路径。
            return None

        active_messages: list[StoredMessage] = [summary_message]  # 先把摘要消息放到首位，保持上下文窗口约定。
        active_indices: list[int] = [summary_state.summary_offset]  # 首位索引对应摘要消息在当前 Redis List 中的绝对位置。
        for relative_index, message in enumerate(ranged_messages):  # 再按原尾段顺序追加除摘要外的所有消息。
            absolute_index = start_offset + relative_index  # 计算当前消息在 Redis List 中的绝对位置。
            if absolute_index == summary_state.summary_offset:  # 摘要消息已经放在首位，不需要重复加入。
                continue
            active_messages.append(message)  # 把保留窗口消息和摘要后的新消息依次追加到输出列表。
            active_indices.append(absolute_index)  # 同步记录对应绝对索引，供压缩策略后续复用。
        return active_messages, active_indices
