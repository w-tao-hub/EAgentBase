"""上下文构建器。

负责将 Agent 配置、历史消息和当前用户消息组装成完整的 LLM 请求上下文。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import inspect
import logging
import re
import time
from typing import TYPE_CHECKING, Protocol

from app.core.models.agent import Agent
from app.core.models.stored_message import StoredMessage
from app.core.runtime.context_history_view import ContextHistoryViewBuilder
from app.core.runtime.context_summary_persistence import (
    SummaryPersistenceCoordinator,
    SummaryPersistencePlan,
)

if TYPE_CHECKING:
    from app.infra.llm.litellm_adapter import LiteLLMAdapter
    from app.infra.store.redis_session_store import ContextSummaryState, RedisSessionStore

logger = logging.getLogger(__name__)

SKILL_MESSAGE_PATTERN = re.compile(
    r"^<skill_name>.*?</skill_name><skill_message>.*?</skill_message>$",
    re.DOTALL,
)

CONTEXT_SUMMARY_PATTERN = re.compile(
    r"^<context_summary>.*?</context_summary>$",
    re.DOTALL,
)


@dataclass(slots=True)
class ContextBuildResult:
    """封装上下文构建结果。"""

    llm_messages: list[dict]
    history_dirty: bool


@dataclass(slots=True)
class TaggedMessageInput:
    """带来源信息的待归一化消息。"""

    message: StoredMessage | dict
    source: str
    history_index: int | None = None
    original_message: StoredMessage | None = None


@dataclass(slots=True)
class NormalizedMessageRecord:
    """单条归一化消息及其来源映射。"""

    llm_message: dict
    source: str
    history_index: int | None = None
    original_message: StoredMessage | None = None


@dataclass(slots=True)
class PreparedContextSnapshot:
    """供压缩策略复用的一次性归一化快照。"""

    llm_messages: list[dict]
    history_dirty: bool
    records: list[NormalizedMessageRecord]


@dataclass(frozen=True, slots=True)
class SummaryPersistenceTarget:
    """摘要持久化目标。

    用于显式区分当前压缩结果属于主会话上下文，还是某个 child 长期上下文。
    """

    kind: str  # main 或 child
    session_id: str
    child_id: str | None = None  # child 目标时必填

    @classmethod
    def for_main(cls, session_id: str) -> "SummaryPersistenceTarget":
        """构造主会话摘要目标。"""
        return cls(kind="main", session_id=session_id)

    @classmethod
    def for_child(cls, session_id: str, child_id: str) -> "SummaryPersistenceTarget":
        """构造 child 长期上下文摘要目标。"""
        return cls(kind="child", session_id=session_id, child_id=child_id)


@dataclass(slots=True)
class PrunedHistoryResult:
    """旧历史清理结果。"""

    records: list[NormalizedMessageRecord]
    removed_query_tool_call_count: int = 0
    removed_query_tool_result_count: int = 0
    removed_skill_message_count: int = 0


class ContextCompressionError(RuntimeError):
    """上下文压缩失败时抛出的稳定异常。"""


class ContextTrimPolicy(Protocol):
    """上下文裁剪策略协议。"""

    async def build_messages(
        self,
        *,
        agent: Agent,
        system_message: StoredMessage,
        history: list[StoredMessage],
        history_indices: list[int] | None = None,
        current_user_message: StoredMessage | None,
        session_id: str | None = None,
        summary_target: SummaryPersistenceTarget | None = None,
        extra_system_messages: list[str] | None = None,
    ) -> list[StoredMessage] | ContextBuildResult:
        """按策略构建上下文消息列表。"""


class NoTrimPolicy:
    """默认不裁剪策略。

    该策略负责按稳定顺序组装默认上下文：
    主 system、附加 system、完整历史、当前用户消息。
    """

    async def build_messages(
        self,
        *,
        agent: Agent,
        system_message: StoredMessage,
        history: list[StoredMessage],
        history_indices: list[int] | None = None,
        current_user_message: StoredMessage | None,
        session_id: str | None = None,
        summary_target: SummaryPersistenceTarget | None = None,
        extra_system_messages: list[str] | None = None,
    ) -> list[StoredMessage]:
        """按默认顺序返回完整上下文消息列表。"""
        del agent, history_indices, session_id
        del summary_target
        messages = self.build_system_messages(system_message, extra_system_messages)
        messages.extend(history)
        if current_user_message is not None:
            messages.append(current_user_message)
        return messages

    @staticmethod
    def build_system_messages(
        system_message: StoredMessage,
        extra_system_messages: list[str] | None,
    ) -> list[StoredMessage]:
        """把主 system 与附加 system 组装成完整前缀。"""
        messages: list[StoredMessage] = [system_message]
        if not extra_system_messages:
            return messages

        for content in extra_system_messages:  # 逐条追加运行时附加的 system 提示。
            if not content:  # 空字符串没有业务意义，直接跳过。
                continue
            messages.append(
                StoredMessage.create(
                    role="system",
                    content=content,
                    timestamp=datetime.now(timezone.utc),
                )
            )
        return messages


class TokenBudgetCompressionPolicy:
    """基于 token 阀值的上下文压缩策略。"""

    def __init__(
        self,
        session_store: "RedisSessionStore",
        llm_adapter: "LiteLLMAdapter",
        token_threshold: int,
    ) -> None:
        """初始化压缩策略。"""
        self._session_store = session_store
        self._summary_persistence = SummaryPersistenceCoordinator(session_store)
        self._llm_adapter = llm_adapter
        self._token_threshold = token_threshold

        from app.core.runtime.context_summary_planner import ContextSummaryPlanner

        self._summary_planner = ContextSummaryPlanner(
            llm_adapter=llm_adapter,
            token_threshold=token_threshold,
        )

    async def build_messages(
        self,
        *,
        agent: Agent,
        system_message: StoredMessage,
        history: list[StoredMessage],
        history_indices: list[int] | None = None,
        current_user_message: StoredMessage | None,
        session_id: str | None = None,
        summary_target: SummaryPersistenceTarget | None = None,
        extra_system_messages: list[str] | None = None,
    ) -> ContextBuildResult:
        """按 token 阀值构建最终上下文消息列表。"""
        # 统一构建 ContextHistoryView：始终通过单一入口获取活动窗口及其绝对索引。
        # 当 history_indices 已由调用方提供时，直接使用外部绝对索引，不基于 summary_state 二次重建。
        summary_state_for_view = None
        if summary_target is not None and history_indices is None:
            if summary_target.kind == "main":
                summary_state_for_view = await self._session_store.get_main_context_summary_state(summary_target.session_id)
            else:
                summary_state_for_view = await self._session_store.get_child_context_summary_state(
                    summary_target.session_id,
                    summary_target.child_id or "",
                )

        history_view = ContextHistoryViewBuilder.from_history(
            history=history,
            summary_state=summary_state_for_view,
            history_indices=history_indices,
        )

        # 基于视图中的活动窗口构建归一化快照，后续压缩决策直接复用。
        full_snapshot = ContextBuilder.prepare_context_snapshot(
            system_message=system_message,
            extra_system_messages=extra_system_messages,
            history=history_view.active_history_messages,
            history_indices=history_view.active_history_indices,
            current_user_message=current_user_message,
        )
        if self._token_threshold <= 0:  # 阀值为 0 时表示关闭压缩能力。
            return ContextBuildResult(
                llm_messages=full_snapshot.llm_messages,
                history_dirty=full_snapshot.history_dirty,
            )

        # 快速估算短路：当估算值明显低于阈值时，跳过昂贵的精确 token 计数。
        rough_tokens = self._rough_token_estimate(full_snapshot.llm_messages)
        # 注意：不再全局跳过 reasoning_content。压缩策略只摘要最近两轮之前的旧历史，
        # 旧助手消息中的 reasoning_content 随旧历史一起被摘要替代；
        # 最近两轮（含 reasoning_content）保持原样传给模型，不会丢失。
        if self._token_threshold >= 5000 and rough_tokens < int(self._token_threshold * 0.7):  # 阈值较大且估算值远低于阈值时，才跳过精确计数，留 30% 安全余量覆盖 tool_calls 与消息格式开销。
            logger.debug(
                "上下文压缩快速估算完成: session_id=%s, stage=skip_by_rough_estimate, rough_tokens=%d, threshold=%d",
                session_id,
                rough_tokens,
                self._token_threshold,
            )
            return ContextBuildResult(
                llm_messages=full_snapshot.llm_messages,
                history_dirty=full_snapshot.history_dirty,
            )

        # 委托摘要规划器完成压缩决策。
        summary_result = await self._summary_planner.plan(
            agent=agent,
            history_view=history_view,
            snapshot=full_snapshot,
        )

        # 无需压缩时直接返回归一化快照。
        if summary_result is None:
            return ContextBuildResult(
                llm_messages=full_snapshot.llm_messages,
                history_dirty=full_snapshot.history_dirty,
            )

        # 仅裁剪不含摘要的情况：从裁剪后的记录重建消息列表。
        if summary_result.summary_message is None:
            final_messages = self._summary_planner.compose_llm_messages_from_records(
                full_snapshot.records,
                override_history_records=summary_result.recent_history_records,
            )
            return ContextBuildResult(
                llm_messages=final_messages,
                history_dirty=full_snapshot.history_dirty,
            )

        # 完整摘要情况：持久化摘要边界并重建消息。
        if summary_target is not None and summary_result.recent_history_records and summary_result.active_start_offset is not None:
            await self._persist_summary(
                summary_target=summary_target,
                summary_message=summary_result.summary_message,
                active_start_message=summary_result.active_start_message,
                active_start_offset=summary_result.active_start_offset,
            )

        final_messages = self._summary_planner.compose_llm_messages_from_records(
            full_snapshot.records,
            override_history_records=[
                NormalizedMessageRecord(
                    llm_message=ContextBuilder.message_to_llm_dict(summary_result.summary_message),
                    source="history_summary",
                ),
                *summary_result.recent_history_records,
            ],
        )
        logger.debug(
            "上下文压缩完成: session_id=%s, stage=summary_applied, final_message_count=%d",
            session_id,
            len(final_messages),
        )
        return ContextBuildResult(
            llm_messages=final_messages,
            history_dirty=full_snapshot.history_dirty,
        )

    async def _persist_summary(
        self,
        *,
        summary_target: SummaryPersistenceTarget,
        summary_message: StoredMessage,
        active_start_message: StoredMessage | None,
        active_start_offset: int | None,
    ) -> None:
        """根据摘要目标委托独立持久化协作者。"""
        await self._summary_persistence.persist(
            SummaryPersistencePlan(
                target=summary_target,
                summary_message=summary_message,
                active_start_message=active_start_message,
                active_start_offset=active_start_offset,
            )
        )

    @staticmethod
    def _rough_token_estimate(messages: list[dict]) -> int:
        """基于字符数做快速 token 估算，用于在明显低于阈值时跳过精确计数。"""
        total_chars = 0  # 累加所有可估算文本的字符数。
        for message in messages:
            content = message.get("content")
            if content:
                total_chars += len(str(content))
            tool_calls = message.get("tool_calls")
            if tool_calls:
                for tool_call in tool_calls:
                    total_chars += len(str(tool_call))
            name = message.get("name")
            if name:
                total_chars += len(str(name))
            tool_call_id = message.get("tool_call_id")
            if tool_call_id:
                total_chars += len(str(tool_call_id))
            reasoning_content = message.get("reasoning_content")
            if reasoning_content:
                total_chars += len(str(reasoning_content))
        return int(total_chars / 3.5)  # 经验系数：对于中英混排和 OpenAI 兼容模型，平均每 token 约 3.5 个字符。



class ContextBuilder:
    """构建 LLM 请求上下文的构建器。

    将 Agent 的系统提示词、会话历史消息和当前用户输入组装成有序的消息列表，
    供后续 LLM 调用使用。所有 TrimPolicy 的产物都会统一经过
    repair-meta 归一化逻辑，保证 assistant/tool 配对语义稳定。
    """

    @classmethod
    async def build(
        cls,
        agent: Agent,
        history: list[StoredMessage],
        history_indices: list[int] | None = None,
        current_user_message: StoredMessage | None = None,
        trim_policy: object | None = None,
        session_id: str | None = None,
        summary_target: SummaryPersistenceTarget | None = None,
        extra_system_messages: list[str] | None = None,
    ) -> list[StoredMessage] | list[dict] | ContextBuildResult:
        """组装完整的上下文消息列表。"""
        system_message = cls._build_system_message(agent)  # 先构造主 system 消息。
        policy = trim_policy or NoTrimPolicy()  # 未显式传入策略时默认使用 NoTrimPolicy。
        effective_summary_target = summary_target
        if effective_summary_target is None and session_id is not None:  # 兼容旧调用点：只给 session_id 时默认视为主会话摘要目标。
            effective_summary_target = SummaryPersistenceTarget.for_main(session_id)
        build_messages_kwargs = {
            "agent": agent,
            "system_message": system_message,
            "history": history,
            "history_indices": history_indices,
            "current_user_message": current_user_message,
            "session_id": session_id,
            "extra_system_messages": extra_system_messages,
        }
        parameter_names = inspect.signature(policy.build_messages).parameters  # 兼容旧自定义 trim policy，不强制要求新增参数。
        if "summary_target" in parameter_names:
            build_messages_kwargs["summary_target"] = effective_summary_target
        return await policy.build_messages(**build_messages_kwargs)  # 所有策略都统一在策略对象内完成上下文拼装。

    @classmethod
    async def build_llm_messages_with_repair_meta(
        cls,
        agent: Agent,
        history: list[StoredMessage],
        history_indices: list[int] | None = None,
        current_user_message: StoredMessage | None = None,
        trim_policy: object | None = None,
        session_id: str | None = None,
        summary_target: SummaryPersistenceTarget | None = None,
        extra_system_messages: list[str] | None = None,
    ) -> ContextBuildResult:
        """构建可直接传给 LLM 的消息列表，并返回脏历史标记。"""
        start_time = time.perf_counter()  # 用高精度时钟统计整个上下文构建方法的真实耗时。
        try:
            built_messages = await cls.build(  # 先通过策略拿到领域消息列表。
                agent=agent,
                history=history,
                history_indices=history_indices,
                current_user_message=current_user_message,
                trim_policy=trim_policy,
                session_id=session_id,
                summary_target=summary_target,
                extra_system_messages=extra_system_messages,
            )
            result = (
                built_messages
                if isinstance(built_messages, ContextBuildResult)  # 压缩策略若已基于预归一化数据产出最终结果，则直接复用。
                else cls.normalize_messages_with_repair_meta(built_messages)  # 其他策略继续统一走 repair-meta 归一化逻辑。
            )
        except Exception as error:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)  # 失败分支同样输出总耗时，便于定位卡点。
            logger.error(
                "上下文构建失败: session_id=%s, history_count=%d, elapsed_ms=%.2f, error=%s",
                session_id,
                len(history),
                elapsed_ms,
                error,
            )
            raise

        # elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)  # 成功分支输出方法整体耗时。
        logger.debug(  # 统一记录归一化结果与总耗时，便于排查上下文压缩行为。
            "上下文消息归一化完成: session_id=%s, history_count=%d, history_dirty=%s, message_count=%d, elapsed_ms=?, messages=%s",
            session_id,
            len(history),
            result.history_dirty,
            len(result.llm_messages),
            # elapsed_ms,
            result.llm_messages,
        )
        return result

    @staticmethod
    def normalize_messages_with_repair_meta(messages: list[dict] | list[StoredMessage]) -> ContextBuildResult:
        """把消息列表转换成 LLM 兼容结构，并在同一次扫描里修复工具配对错乱。"""
        snapshot = ContextBuilder._normalize_tagged_messages_with_repair_meta(
            tagged_messages=[
                TaggedMessageInput(
                    message=message,
                    source="unknown",
                    original_message=message if isinstance(message, StoredMessage) else None,
                )
                for message in messages
            ]
        )
        return ContextBuildResult(
            llm_messages=snapshot.llm_messages,
            history_dirty=snapshot.history_dirty,
        )

    @staticmethod
    def message_to_llm_dict(message: StoredMessage) -> dict:
        """把单条 StoredMessage 转成 LLM 兼容结构。"""
        msg_dict: dict = {"role": message.role}
        if message.content is not None:
            msg_dict["content"] = message.content
        if message.tool_calls is not None:
            msg_dict["tool_calls"] = message.tool_calls
        if message.reasoning_content is not None:
            msg_dict["reasoning_content"] = message.reasoning_content
        if message.tool_call_id is not None:
            msg_dict["tool_call_id"] = message.tool_call_id
        if message.name is not None:
            msg_dict["name"] = message.name
        return msg_dict

    @staticmethod
    def _build_synthetic_assistant_tool_call_message(tool_call_id: str, tool_name: str | None) -> dict:
        """为孤儿 tool 结果构造一条最小可消费的 assistant(tool_calls) 消息。"""
        return {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": tool_name or "unknown_tool",
                        "arguments": "{}",
                    },
                }
            ],
        }

    @staticmethod
    def prepare_context_snapshot(
        *,
        system_message: StoredMessage,
        extra_system_messages: list[str] | None,
        history: list[StoredMessage],
        history_indices: list[int] | None = None,
        current_user_message: StoredMessage | None,
    ) -> PreparedContextSnapshot:
        """一次性归一化完整上下文，并保留来源与索引映射。"""
        tagged_messages: list[TaggedMessageInput] = []
        for index, prepared_system_message in enumerate(
            NoTrimPolicy.build_system_messages(system_message, extra_system_messages)
        ):
            tagged_messages.append(
                TaggedMessageInput(
                    message=prepared_system_message,
                    source="system" if index == 0 else "extra_system",
                    original_message=prepared_system_message,
                )
            )
        # 精确判断 history_indices：None 时退化 range(len(history))；
        # 空列表与长度不一致时直接报错，避免静默截断或误退化为本地索引。
        if history_indices is None:
            resolved_history_indices = list(range(len(history)))
        elif len(history_indices) != len(history):
            raise ValueError(
                f"history_indices 长度与 history 不一致: "
                f"{len(history_indices)} vs {len(history)}"
            )
        else:
            resolved_history_indices = history_indices
        for history_index, history_message in zip(resolved_history_indices, history):
            tagged_messages.append(
                TaggedMessageInput(
                    message=history_message,
                    source="history",
                    history_index=history_index,
                    original_message=history_message,
                )
            )
        if current_user_message is not None:
            tagged_messages.append(
                TaggedMessageInput(
                    message=current_user_message,
                    source="current_user",
                    original_message=current_user_message,
                )
            )

        return ContextBuilder._normalize_tagged_messages_with_repair_meta(tagged_messages)

    @staticmethod
    def _normalize_tagged_messages_with_repair_meta(
        tagged_messages: list[TaggedMessageInput],
    ) -> PreparedContextSnapshot:
        """把带来源信息的消息列表归一化为 LLM 结构，并保留来源映射。"""
        if not tagged_messages:  # 空列表直接返回，避免后续不必要处理。
            return PreparedContextSnapshot(llm_messages=[], history_dirty=False, records=[])

        llm_messages: list[dict] = []
        records: list[NormalizedMessageRecord] = []
        history_dirty = False  # 记录本次扫描是否发现了需要修复的历史错乱。
        pending_assistant: dict | None = None

        def flush_pending_assistant() -> None:
            """把上一批 assistant tool 请求按已匹配结果结算到输出列表。"""
            nonlocal pending_assistant, history_dirty
            if pending_assistant is None:  # 没有待结算批次时直接返回。
                return

            matched_tool_call_ids = pending_assistant["matched_tool_call_ids"]  # 读取本批已匹配的 tool_call_id 集合。
            original_tool_calls = pending_assistant["tool_calls"]  # 读取本批 assistant 原始 tool_calls。
            kept_tool_calls = [  # 只保留真正拿到 tool 结果的 tool_calls。
                tool_call
                for tool_call in original_tool_calls
                if tool_call.get("id") in matched_tool_call_ids
            ]

            if len(kept_tool_calls) != len(original_tool_calls):  # 只要有 tool_call 被裁掉，就说明历史存在错乱。
                history_dirty = True

            assistant_content = pending_assistant["content"]  # 读取 assistant 原始文本内容。
            assistant_reasoning_content = pending_assistant["reasoning_content"]  # 读取 assistant 原始 reasoning_content。
            if kept_tool_calls:  # 还有有效 tool_calls 时，输出裁剪后的 assistant(tool_calls)。
                assistant_message = {"role": "assistant"}  # assistant 角色固定为 assistant。
                if assistant_content is not None:  # 仅在有文本时才输出 content，避免写入无意义空字段。
                    assistant_message["content"] = assistant_content
                if assistant_reasoning_content is not None:  # reasoning_content 必须跟随 assistant 一起保留，供后续 user 轮次继续回传。
                    assistant_message["reasoning_content"] = assistant_reasoning_content
                assistant_message["tool_calls"] = kept_tool_calls
                llm_messages.append(assistant_message)
                records.append(
                    NormalizedMessageRecord(
                        llm_message=assistant_message,
                        source=pending_assistant["source"],
                        history_index=pending_assistant["history_index"],
                        original_message=pending_assistant["original_message"],
                    )
                )
                for matched_record in pending_assistant["matched_tool_records"]:
                    llm_messages.append(matched_record.llm_message)  # assistant 必须先于对应 tool 结果出现。
                    records.append(matched_record)
            elif assistant_content is not None:  # tool_calls 全部失效但还有文本时，降级为普通 assistant。
                assistant_message = {"role": "assistant", "content": assistant_content}
                if assistant_reasoning_content is not None:  # 降级为普通 assistant 时，同样不能丢掉 reasoning_content。
                    assistant_message["reasoning_content"] = assistant_reasoning_content
                llm_messages.append(assistant_message)
                records.append(
                    NormalizedMessageRecord(
                        llm_message=assistant_message,
                        source=pending_assistant["source"],
                        history_index=pending_assistant["history_index"],
                        original_message=pending_assistant["original_message"],
                    )
                )

            pending_assistant = None  # 当前批次已结算，清空待决状态。

        for tagged_message in tagged_messages:  # 在同一次循环里完成 dict 转换、工具配对修复与 dirty 标记。
            raw_message = tagged_message.message
            message_dict = (
                dict(raw_message)
                if isinstance(raw_message, dict)
                else ContextBuilder.message_to_llm_dict(raw_message)
            )
            role = message_dict.get("role")  # 读取当前消息角色。

            if role == "assistant" and message_dict.get("tool_calls"):  # assistant 工具请求需要延后到看到 tool 结果后再决定是否保留。
                flush_pending_assistant()  # 先结算上一批，确保批次边界稳定。
                pending_assistant = {
                    "content": message_dict.get("content"),
                    "reasoning_content": message_dict.get("reasoning_content"),
                    "tool_calls": list(message_dict["tool_calls"]),
                    "matched_tool_call_ids": set(),
                    "matched_tool_records": [],
                    "source": tagged_message.source,
                    "history_index": tagged_message.history_index,
                    "original_message": tagged_message.original_message,
                }
                continue

            if role == "tool":  # tool 消息需要和上一批 assistant tool 请求做配对。
                tool_call_id = message_dict.get("tool_call_id")  # 读取当前 tool 消息的 tool_call_id。
                if not tool_call_id:  # 缺少 tool_call_id 的 tool 消息无法合法回放。
                    history_dirty = True  # 标记历史有问题，后续只打脏标记不回写。
                    continue

                if pending_assistant is not None:  # 若当前存在待配对 assistant 批次，优先尝试就地匹配。
                    pending_tool_call_ids = {
                        tool_call.get("id")
                        for tool_call in pending_assistant["tool_calls"]
                    }
                    if tool_call_id in pending_tool_call_ids:  # 命中当前批次时，保留原 tool 消息并记录匹配成功。
                        pending_assistant["matched_tool_call_ids"].add(tool_call_id)
                        pending_assistant["matched_tool_records"].append(
                            NormalizedMessageRecord(
                                llm_message=message_dict,
                                source=tagged_message.source,
                                history_index=tagged_message.history_index,
                                original_message=tagged_message.original_message,
                            )
                        )  # 先缓存 tool，等 flush 时再放到 assistant 后面。
                        continue

                flush_pending_assistant()  # 遇到孤儿 tool 前，先把上一批 assistant tool 批次结算掉。
                history_dirty = True  # 当前 tool 没有对应 assistant 请求，说明历史存在错乱。
                synthetic_assistant = ContextBuilder._build_synthetic_assistant_tool_call_message(
                    tool_call_id=tool_call_id,
                    tool_name=message_dict.get("name"),
                )
                llm_messages.append(synthetic_assistant)  # 先补一条合成 assistant(tool_calls)，让当前 tool 能被模型合法消费。
                records.append(
                    NormalizedMessageRecord(
                        llm_message=synthetic_assistant,
                        source=tagged_message.source,
                        history_index=tagged_message.history_index,
                        original_message=tagged_message.original_message,
                    )
                )
                llm_messages.append(message_dict)  # 再追加原始 tool 结果，保持用户可见结果不丢失。
                records.append(
                    NormalizedMessageRecord(
                        llm_message=message_dict,
                        source=tagged_message.source,
                        history_index=tagged_message.history_index,
                        original_message=tagged_message.original_message,
                    )
                )
                continue

            flush_pending_assistant()  # 普通消息会切断上一批工具调用窗口，因此要先结算待决批次。
            llm_messages.append(message_dict)  # 当前消息本身无需额外修复，直接进入输出。
            records.append(
                NormalizedMessageRecord(
                    llm_message=message_dict,
                    source=tagged_message.source,
                    history_index=tagged_message.history_index,
                    original_message=tagged_message.original_message,
                )
            )

        flush_pending_assistant()  # 处理列表尾部残留的 assistant tool 批次。
        return PreparedContextSnapshot(
            llm_messages=llm_messages,
            history_dirty=history_dirty,
            records=records,
        )

    @staticmethod
    def _build_system_message(agent: Agent) -> StoredMessage:
        """构造 system 消息。"""
        return StoredMessage.create(
            role="system",
            content=agent.system_prompt,
            timestamp=datetime.now(timezone.utc),
        )
