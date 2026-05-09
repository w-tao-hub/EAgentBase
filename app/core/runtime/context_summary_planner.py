"""上下文摘要规划器。

负责在上下文超出 token 阈值时，决定如何压缩上下文。
将 TokenBudgetCompressionPolicy 中的摘要规划逻辑提取为独立模块。

当前规划器只处理 TokenBudgetCompressionPolicy 遗留的固定策略：
* 清理旧历史中的 query_tool_result / skill 注入痕迹
* 对旧历史生成模型压缩摘要
* 保留最近两轮完整会话
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import TYPE_CHECKING

from app.core.models.agent import Agent
from app.core.models.stored_message import StoredMessage
from app.core.runtime.context_builder import (
    ContextBuilder,
    ContextCompressionError,
    NormalizedMessageRecord,
    PreparedContextSnapshot,
    PrunedHistoryResult,
    SKILL_MESSAGE_PATTERN,
)
from app.core.runtime.context_history_view import ContextHistoryView

if TYPE_CHECKING:
    from app.infra.llm.litellm_adapter import LiteLLMAdapter

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SummaryCompressionResult:
    """摘要压缩结果。

    summary_message 为 None 时表示仅做了历史裁剪但没有生成摘要，
    此时 recent_history_records 包含所有裁剪后保留的历史记录。
    """

    summary_message: StoredMessage | None
    recent_history_records: list[NormalizedMessageRecord]
    active_start_message: StoredMessage | None
    active_start_offset: int | None


class ContextSummaryPlanner:
    """上下文摘要规划器。

    当上下文超过 token 阈值时，规划如何压缩上下文，
    包括清理旧历史、生成摘要等。
    """

    def __init__(
        self,
        llm_adapter: LiteLLMAdapter,
        token_threshold: int,
    ) -> None:
        """初始化摘要规划器。

        Args:
            llm_adapter: 用于 token 统计和摘要生成的 LiteLLM 适配器。
            token_threshold: 触发压缩的输入 token 阈值。
        """
        self._llm_adapter = llm_adapter
        self._token_threshold = token_threshold
        self._post_cleanup_ratio = 2 / 3
        self._summary_target_tokens = 10_000
        self._summary_max_tokens = 11_000

    async def plan(
        self,
        *,
        agent: Agent,
        history_view: ContextHistoryView,
        snapshot: PreparedContextSnapshot,
    ) -> SummaryCompressionResult | None:
        """规划上下文压缩方案。

        压缩只影响最近两轮之前的旧历史（older_history_records），这些旧消息中的
        reasoning_content 随旧历史一起被摘要替代；最近两轮（含 reasoning_content）
        保持原样，不会被破坏。

        依次执行以下步骤：
        1. 检查上下文 token 是否超过阈值，未超过则跳过。
        2. 找出最近两轮完整会话的起点。
        3. 计算在完整历史中的绝对索引。
        4. 清理旧历史中的 query_tool_result 和 skill 注入痕迹。
        5. 重建清理后的消息列表并检查 token 是否降到阈值以下。
        6. 分离旧历史记录和最近两轮记录。
        7. 检查是否存在可压缩旧历史。
        8. 对旧历史生成压缩摘要。
        9. 检查摘要自身的 token 规模是否超过上限。

        Args:
            agent: 当前 Agent 配置，用于模型选择和摘要调用。
            history_view: 包含活动窗口与完整历史映射的视图。
            snapshot: 已归一化的上下文快照，包含 llm_messages 与 records。

        Returns:
            SummaryCompressionResult 或 None（无需压缩时返回 None）。
        """

        # 1. 检查上下文 token 是否超过阈值。
        prompt_tokens = await self._count_prompt_tokens(agent.model, snapshot.llm_messages)
        if prompt_tokens <= self._token_threshold:
            logger.debug(
                "上下文压缩检查完成: stage=skip_below_threshold, prompt_tokens=%d, threshold=%d",
                prompt_tokens,
                self._token_threshold,
            )
            return None

        # 2. 找出最近两轮完整会话的起点。
        logger.info(
            "上下文压缩触发: stage=threshold_exceeded, prompt_tokens=%d, threshold=%d, history_count=%d",
            prompt_tokens,
            self._token_threshold,
            len(history_view.active_history_messages),
        )
        keep_start = self._find_keep_start_index(history_view.active_history_messages)

        # 3. 计算在完整历史中的绝对索引。
        keep_history_start_index = (
            history_view.active_history_indices[keep_start]
            if keep_start < len(history_view.active_history_indices)
            else None
        )

        # 4. 清理旧历史记录。
        prune_result = self._prune_old_history_records(
            records=snapshot.records,
            keep_history_start_index=keep_history_start_index,
        )
        pruned_history_records = prune_result.records
        logger.info(
            "上下文旧历史清理完成: removed_query_tool_calls=%d, removed_query_tool_results=%d, removed_skill_messages=%d",
            prune_result.removed_query_tool_call_count,
            prune_result.removed_query_tool_result_count,
            prune_result.removed_skill_message_count,
        )

        # 5. 重建清理后的消息并检查 token。
        pruned_messages = self.compose_llm_messages_from_records(pruned_history_records)
        pruned_prompt_tokens = await self._count_prompt_tokens(agent.model, pruned_messages)
        if pruned_prompt_tokens <= int(self._token_threshold * self._post_cleanup_ratio):
            logger.debug(
                "上下文压缩检查完成: stage=skip_after_cleanup, prompt_tokens=%d, threshold=%d, cleanup_threshold=%d",
                pruned_prompt_tokens,
                self._token_threshold,
                int(self._token_threshold * self._post_cleanup_ratio),
            )
            # 仅裁剪未摘要：返回所有裁剪后的历史记录（不带系统/用户前缀，由调用方通过 compose 组装）。
            return SummaryCompressionResult(
                summary_message=None,
                recent_history_records=[
                    r
                    for r in pruned_history_records
                    if r.source == "history"
                ],
                active_start_message=None,
                active_start_offset=None,
            )

        # 6. 分离旧历史记录和最近两轮记录。
        older_history_records = [
            record
            for record in pruned_history_records
            if record.source == "history"
            and record.history_index is not None
            and keep_history_start_index is not None
            and record.history_index < keep_history_start_index
        ]
        recent_history_records = [
            record
            for record in pruned_history_records
            if record.source == "history"
            and (
                keep_history_start_index is None
                or record.history_index is None
                or record.history_index >= keep_history_start_index
            )
        ]

        # 7. 检查是否存在可压缩旧历史。
        if not older_history_records:
            logger.error(
                "上下文压缩失败: stage=no_compressible_history, prompt_tokens=%d, threshold=%d",
                pruned_prompt_tokens,
                self._token_threshold,
            )
            raise ContextCompressionError("上下文超过阈值，但除最近两轮外没有可压缩历史")

        # 8. 对旧历史生成压缩摘要。
        logger.info(
            "上下文压缩继续执行摘要: stage=generate_summary, prompt_tokens=%d, threshold=%d, older_record_count=%d, recent_record_count=%d",
            pruned_prompt_tokens,
            self._token_threshold,
            len(older_history_records),
            len(recent_history_records),
        )
        summary_message = await self._generate_summary_message(
            agent=agent,
            records=older_history_records,
        )

        # 9. 检查摘要自身的 token 规模。
        summary_token_count = await self._count_summary_tokens(agent.model, summary_message)
        if summary_token_count > self._summary_max_tokens:
            logger.error(
                "上下文压缩失败: stage=summary_too_large, summary_tokens=%d, max_tokens=%d",
                summary_token_count,
                self._summary_max_tokens,
            )
            raise ContextCompressionError(
                f"压缩摘要超过允许上限: summary_tokens={summary_token_count}, max_tokens={self._summary_max_tokens}"
            )

        # 10. 构造压缩结果。
        active_start_message = (
            history_view.active_history_messages[keep_start]
            if keep_start < len(history_view.active_history_messages)
            else None
        )
        return SummaryCompressionResult(
            summary_message=summary_message,
            recent_history_records=recent_history_records,
            active_start_message=active_start_message,
            active_start_offset=keep_history_start_index,
        )

    # 私有辅助方法

    @staticmethod
    def compose_llm_messages_from_records(
        base_records: list[NormalizedMessageRecord],
        override_history_records: list[NormalizedMessageRecord] | None = None,
    ) -> list[dict]:
        """基于归一化记录直接重建最终发送给模型的消息列表。"""
        prefix_records = [
            record
            for record in base_records
            if record.source in {"system", "extra_system"}
        ]
        suffix_records = [
            record
            for record in base_records
            if record.source == "current_user"
        ]
        history_records = (
            override_history_records
            if override_history_records is not None
            else [
                record
                for record in base_records
                if record.source == "history"
            ]
        )
        return [
            *(record.llm_message for record in prefix_records),
            *(record.llm_message for record in history_records),
            *(record.llm_message for record in suffix_records),
        ]

    @staticmethod
    def _find_keep_start_index(history: list[StoredMessage]) -> int:
        """找出最近两轮完整会话的起点索引。"""
        round_start_indices = [
            index
            for index, message in enumerate(history)
            if message.role == "user" and not message.is_meta
        ]
        if len(round_start_indices) <= 2:
            return 0
        return round_start_indices[-2]

    @staticmethod
    def _prune_old_history_records(
        records: list[NormalizedMessageRecord],
        keep_history_start_index: int | None,
    ) -> PrunedHistoryResult:
        """删除最近两轮之前的旧 query_tool_result 痕迹和旧 skill 注入消息。"""
        removed_tool_call_ids: set[str] = set()
        pruned_records: list[NormalizedMessageRecord] = []
        removed_query_tool_call_count = 0
        removed_query_tool_result_count = 0
        removed_skill_message_count = 0

        for record in records:
            if record.source != "history":
                pruned_records.append(record)
                continue
            if (
                keep_history_start_index is not None
                and record.history_index is not None
                and record.history_index >= keep_history_start_index
            ):
                pruned_records.append(record)
                continue

            if ContextSummaryPlanner._is_skill_injection_record(record):
                removed_skill_message_count += 1
                continue

            role = str(record.llm_message.get("role", ""))
            if role == "assistant" and record.llm_message.get("tool_calls"):
                kept_tool_calls = []
                for tool_call in record.llm_message["tool_calls"]:
                    tool_name = str(tool_call.get("function", {}).get("name", ""))
                    if tool_name == "query_tool_result":
                        tool_call_id = str(tool_call.get("id", ""))
                        if tool_call_id:
                            removed_tool_call_ids.add(tool_call_id)
                        removed_query_tool_call_count += 1
                        continue
                    kept_tool_calls.append(tool_call)

                if not kept_tool_calls and not record.llm_message.get("content"):
                    continue

                updated_message = dict(record.llm_message)
                updated_message["tool_calls"] = kept_tool_calls or None
                if kept_tool_calls is None or not kept_tool_calls:
                    updated_message.pop("tool_calls", None)
                pruned_records.append(
                    NormalizedMessageRecord(
                        llm_message=updated_message,
                        source=record.source,
                        history_index=record.history_index,
                        original_message=record.original_message,
                    )
                )
                continue

            if role == "tool" and str(record.llm_message.get("tool_call_id", "")) in removed_tool_call_ids:
                removed_query_tool_result_count += 1
                continue

            pruned_records.append(record)

        return PrunedHistoryResult(
            records=pruned_records,
            removed_query_tool_call_count=removed_query_tool_call_count,
            removed_query_tool_result_count=removed_query_tool_result_count,
            removed_skill_message_count=removed_skill_message_count,
        )

    @staticmethod
    def _is_skill_injection_message(message: StoredMessage) -> bool:
        """判断当前消息是否为隐藏的 skill 注入消息。"""
        if message.role != "user":
            return False
        if not message.is_meta:
            return False
        if not isinstance(message.content, str):
            return False
        return bool(SKILL_MESSAGE_PATTERN.match(message.content))

    @staticmethod
    def _is_skill_injection_record(record: NormalizedMessageRecord) -> bool:
        """判断归一化记录是否对应隐藏的 skill 注入消息。"""
        if record.original_message is None:
            return False
        return ContextSummaryPlanner._is_skill_injection_message(record.original_message)

    async def _generate_summary_message(
        self,
        agent: Agent,
        records: list[NormalizedMessageRecord],
    ) -> StoredMessage:
        """对旧历史生成新的隐藏摘要消息。"""
        summary_prompt_messages = self._build_summary_prompt_messages(records)
        last_error: Exception | None = None

        for attempt in range(2):
            try:
                summary_text = await self._llm_adapter.complete_text(
                    model=agent.model,
                    messages=summary_prompt_messages,
                    temperature=0.0,
                    enable_thinking=False,
                )
                normalized_summary = summary_text.strip()
                if not normalized_summary:
                    raise ValueError("模型未返回有效的上下文摘要")
                return StoredMessage.create(
                    role="user",
                    content=f"<context_summary>{normalized_summary}</context_summary>",
                    timestamp=datetime.now(timezone.utc),
                    is_meta=True,
                )
            except Exception as error:
                last_error = error
                logger.warning("上下文摘要生成失败: attempt=%d, error=%s", attempt + 1, error)

        raise ContextCompressionError(f"上下文摘要生成失败: {last_error}")

    def _build_summary_prompt_messages(self, records: list[NormalizedMessageRecord]) -> list[dict[str, str]]:
        """构造上下文摘要模型调用的提示词消息。"""
        serialized_history = self._serialize_records_for_summary(records)
        return [
            {
                "role": "system",
                "content": (
                    "你负责压缩历史对话上下文。"
                    "输出必须忠实、精炼、不可引入新信息。"
                    f"摘要目标控制在 {self._summary_target_tokens} token 左右，不要无谓展开。"
                    "摘要必须覆盖：当前目标、已确认约束、关键工具结论、未完成事项。"
                    "只输出摘要正文，不要输出标题、解释、Markdown 列表或任何 XML 标签。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "请将以下较早的会话历史压缩为一段可供后续轮次继续推理的上下文摘要：\n"
                    f"{serialized_history}"
                ),
            },
        ]

    @staticmethod
    def _serialize_records_for_summary(records: list[NormalizedMessageRecord]) -> str:
        """把归一化历史记录转成模型可读的稳定文本。"""
        lines: list[str] = []
        for index, record in enumerate(records, start=1):
            llm_message = record.llm_message
            original_message = record.original_message
            header = (
                f"[{index}] role={llm_message.get('role', '')} "
                f"is_meta={original_message.is_meta if original_message is not None else False} "
                f"name={llm_message.get('name', '')} "
                f"tool_call_id={llm_message.get('tool_call_id', '')}"
            )
            lines.append(header)
            lines.append(f"content={llm_message.get('content', '') or ''}")
            if llm_message.get("tool_calls"):
                lines.append(f"tool_calls={llm_message['tool_calls']}")
            lines.append("")
        return "\n".join(lines)

    async def _count_summary_tokens(self, model: str, summary_message: StoredMessage) -> int:
        """统计压缩摘要消息自身的 token 数。"""
        llm_message = ContextBuilder.message_to_llm_dict(summary_message)
        return await self._llm_adapter.count_prompt_tokens(model=model, messages=[llm_message])

    async def _count_prompt_tokens(
        self,
        model: str,
        llm_messages: list[dict],
    ) -> int:
        """统计上下文的输入 token 数。"""
        return await self._llm_adapter.count_prompt_tokens(model=model, messages=llm_messages)
