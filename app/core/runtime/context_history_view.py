"""历史视图模块。

负责把输入历史与摘要状态组合成稳定的活动窗口视图。
source_history_* 承载输入片段及其绝对索引映射，不保证是 Redis 全量历史；
active_history_* 承载真正送入 LLM 的活动窗口子集。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.models.stored_message import StoredMessage

if TYPE_CHECKING:
    from app.infra.store.redis_session_store import ContextSummaryState


@dataclass(slots=True)
class ContextHistoryView:
    """统一表达输入历史片段与活动窗口。

    source_history_* 表示输入给 builder 的那一段消息及其绝对索引映射，
    不承诺一定是 Redis 中的全量完整历史（当 history_indices 非空时，输入为已裁剪的活动窗口）。
    active_history_* 表示经过摘要压缩后、真正会送入 LLM 的消息子集及其在输入历史中的绝对索引。
    """

    source_history_messages: list[StoredMessage]
    source_history_indices: list[int]
    active_history_messages: list[StoredMessage]
    active_history_indices: list[int]


class ContextHistoryViewBuilder:
    """历史视图构建器。

    负责将完整历史与摘要状态组合成稳定的活动窗口视图。
    不依赖 Redis I/O，仅基于内存中的历史列表和摘要状态工作。
    """

    @classmethod
    def from_history(
        cls,
        *,
        history: list[StoredMessage],
        summary_state: "ContextSummaryState | None",
        history_indices: list[int] | None = None,
    ) -> ContextHistoryView:
        """根据完整历史和摘要状态重建活动窗口。

        优先级规则：
        - 当 history_indices is not None 时，输入 history 已是当前逻辑活动窗口，
          builder 不基于 summary_state 二次重建活动窗口；
          直接使用 history_indices 作为 source_history_indices（summary_state=None 时也是 active_history_indices）。
        - 只有 history_indices is None 时，才允许结合 summary_state 从完整历史重建活动窗口。

        Args:
            history: 完整历史消息列表（或已裁剪的活动窗口，当 history_indices 非空时）。
            summary_state: 最近一次摘要边界状态；为 None 表示从未压缩过。
            history_indices: 完整历史中的绝对索引映射；为 None 时退化为 range(len(history))。
                非空时，其长度必须与 history 一致。

        Returns:
            ContextHistoryView: 包含完整历史与活动窗口的统一视图。
        """
        source_history_messages = list(history)  # 复制输入历史，避免外部调用方意外修改。

        if history_indices is not None:  # 调用方提供了外部绝对索引映射，直接使用，不再二次重建。
            if len(history_indices) != len(history):  # builder 层守住长度不变量，不依赖上游兜底。
                raise ValueError(
                    f"history_indices 长度与 history 不一致: "
                    f"{len(history_indices)} vs {len(history)}"
                )

            source_history_indices = list(history_indices)  # 复制外部索引，避免被意外修改。
            # 当 history_indices 非空时，输入 history 已被调用方视为当前逻辑活动窗口；
            # 即使 summary_state 存在，也不能再基于它二次重建活动窗口。
            if summary_state is None:  # 未压缩过时，活动窗口就是当前传入的完整历史。
                return ContextHistoryView(
                    source_history_messages=source_history_messages,
                    source_history_indices=source_history_indices,
                    active_history_messages=list(history),
                    active_history_indices=list(history_indices),
                )
        else:
            source_history_indices = list(range(len(history)))  # 未提供外部索引时，退化为本地顺序编号。

        # 若未提供外部索引且 summary_state 为 None，活动窗口 = 完整历史。
        # 若提供了外部索引（history_indices is not None），输入 history 已是活动窗口，
        # 不基于 summary_state 二次重建。
        if summary_state is None or history_indices is not None:
            return ContextHistoryView(
                source_history_messages=source_history_messages,
                source_history_indices=source_history_indices,
                active_history_messages=list(history),
                active_history_indices=list(source_history_indices),
            )

        # 以下：history_indices is None 且 summary_state 非空，
        # 按 UUID 扫描定位摘要消息和活动窗口起点在完整历史中的位置。
        summary_index: int | None = None
        active_start_index: int | None = None
        for index, message in enumerate(history):
            if message.message_id == summary_state.summary_message_id:
                summary_index = index
            if (  # 活动窗口起点 message_id 非空时才扫描定位。
                summary_state.active_start_message_id is not None
                and message.message_id == summary_state.active_start_message_id
            ):
                active_start_index = index

        if summary_index is None:  # 摘要消息未找到时回退完整历史。
            return ContextHistoryView(
                source_history_messages=source_history_messages,
                source_history_indices=source_history_indices,
                active_history_messages=list(history),
                active_history_indices=list(source_history_indices),
            )

        if active_start_index is not None and (active_start_index < 0 or active_start_index > summary_index):  # 起点位置非法时的防御性边界检查，回退到完整历史保持向后兼容。
            return ContextHistoryView(
                source_history_messages=source_history_messages,
                source_history_indices=source_history_indices,
                active_history_messages=list(history),
                active_history_indices=list(source_history_indices),
            )

        # 构建活动窗口：摘要消息先行，然后是从起点到末尾的所有非摘要消息。
        active_history_messages: list[StoredMessage] = [history[summary_index]]
        active_history_indices: list[int] = [summary_index]
        start_index = (
            active_start_index
            if active_start_index is not None and active_start_index <= summary_index
            else summary_index + 1
        )  # 活动窗口起点：优先使用摘要前保留窗口的起点，否则从摘要下一个位置开始。
        for index in range(start_index, len(history)):
            if index == summary_index:  # 摘要消息已放在首位，不需要重复加入。
                continue
            active_history_messages.append(history[index])
            active_history_indices.append(index)

        return ContextHistoryView(
            source_history_messages=source_history_messages,
            source_history_indices=source_history_indices,
            active_history_messages=active_history_messages,
            active_history_indices=active_history_indices,
        )
