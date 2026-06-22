"""跨 Store 复合写入端口。

业务层中的很多写操作需要同时更新多个 Store（如创建 Run 的同时还要把 run_id
写入 session run 索引），如果让每个调用点自己编排这些复合操作，会导致大量
重复代码和潜在的不一致风险。该模块把这种"一次业务操作对应多次存储写入"的
场景抽象为独立的复合写入端口，实现方可以根据后端能力选择用 pipeline 或
事务来保证原子性。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.core.models.error import ErrorCode
from app.core.models.run import Run, RunStatus
from app.core.models.stored_message import StoredMessage


@dataclass(frozen=True, slots=True)
class RunCreateWrite:
    """创建 Run 并写入 session run 索引的复合写入请求。

    一次 Run 创建对应两个存储操作：
    1. 在 RunStore 中创建 Run 记录
    2. 在 SessionStore 中将该 run_id 追加到 session run 索引
    这两个操作必须同时成功或同时失败，否则会出现"有 Run 记录但无法
    按 session 查到"的数据不一致。使用不可变 frozen dataclass 设计
    是因为该请求一旦构造就不应再被修改，避免请求在传递过程中被意外篡改。
    """

    session_id: str
    run: Run
    run_ttl_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class MainRunTerminalWrite:
    """主 Run 终态和可选主上下文终态消息写入请求。

    当主 Run 结束时需要：
    1. 更新 Run 的状态为终态并写入结束时间
    2. 如果产生了终态消息（如最终的 assistant 回复），将该消息追加到
       主会话上下文
    这两个操作的原子性很重要：如果只写了终态但消息没写成功，用户看到
    的状态是"已完成"但最后一条消息不见了。
    terminal_message 是可选的，因为某些终态（如 CANCELLED）可能没有
    对应的终态消息。
    """

    session_id: str
    run_id: str
    status: RunStatus
    finished_at: datetime
    output: str | None = None
    error_code: ErrorCode | None = None
    error_message: str | None = None
    terminal_message: StoredMessage | None = None


@dataclass(frozen=True, slots=True)
class ChildContextStartWrite:
    """child 首条输入消息和可恢复摘要写入请求。

    当子代理开始执行时，需要同时完成：
    1. 将用户的第一条消息写入 child 上下文
    2. 更新 child 的可恢复摘要（方便后续中断后恢复）
    这两个操作放在一起的原因是：如果消息写入成功但摘要更新失败，
    下次恢复时可能定位不到正确的起始位置。分开写入会导致状态不一致。
    """

    session_id: str
    child_id: str
    child_run_id: str
    user_message: StoredMessage
    subagent_type: str
    description: str


@dataclass(frozen=True, slots=True)
class ChildRunTerminalWrite:
    """child Run 终态和可选 child 上下文终态消息写入请求。

    与 MainRunTerminalWrite 对称但多了 child_id 和 subagent_type：
    - child_id 用于定位 child 上下文
    - subagent_type 用于在最后更新可恢复摘要时记录 child 类型
    这种对称设计使得主 Run 和 child Run 的终态处理逻辑在模式上保持一致，
    降低了理解和维护成本。
    """

    session_id: str
    child_id: str
    child_run_id: str
    status: RunStatus
    finished_at: datetime
    subagent_type: str | None = None
    output: str | None = None
    error_code: ErrorCode | None = None
    error_message: str | None = None
    terminal_message: StoredMessage | None = None


class StoreTransaction(Protocol):
    """业务语义级复合写入端口，不暴露 Redis pipeline。

    该端口定义的是"业务操作级别"的原子写入单元，而不是"存储级别"的
    原子操作。它让调用方不需要知道底层的 Store 结构——调用方只需要说
    "我要创建一个 Run 并把它注册到 session"就够了，具体怎么实现
    原子性（Redis pipeline / DB transaction / 补偿机制）是实现方的责任。
    """

    async def create_run_and_index_session(self, write: RunCreateWrite) -> None:
        """创建 Run，并把 run_id 写入 session run 索引。"""

    async def persist_main_run_terminal(self, write: MainRunTerminalWrite) -> None:
        """持久化主 Run 终态，并按需追加主上下文终态消息。"""

    async def append_child_input_and_summary(self, write: ChildContextStartWrite) -> None:
        """写入 child 首条用户消息，并覆盖 child 可恢复摘要。"""

    async def persist_child_run_terminal(self, write: ChildRunTerminalWrite) -> None:
        """持久化 child Run 终态，并按需追加 child 终态消息。"""
