"""核心端口统一导出。

端口（Port）是 DIP（依赖倒置原则）中的"抽象"层——
业务层依赖于这里定义的 Protocol 和 dataclass，
而具体的存储实现（如 RedisSessionStore）则位于 infra 层。
这种分层确保替换存储实现时不需要修改业务层代码。
"""

from app.core.ports.cancellation import RunCancelBus
from app.core.ports.stores import (
    ContextSummaryState,
    LockStore,
    PersistedToolResult,
    RunStore,
    SessionChildSummary,
    SessionStore,
    TaskStore,
    ToolResultPersistenceStore,
    ToolResultStore,
)
from app.core.ports.transactions import (
    ChildContextStartWrite,
    ChildRunTerminalWrite,
    MainRunTerminalWrite,
    RunCreateWrite,
    StoreTransaction,
)

__all__ = [
    "ChildContextStartWrite",
    "ChildRunTerminalWrite",
    "ContextSummaryState",
    "LockStore",
    "MainRunTerminalWrite",
    "PersistedToolResult",
    "RunCancelBus",
    "RunCreateWrite",
    "RunStore",
    "SessionChildSummary",
    "SessionStore",
    "StoreTransaction",
    "TaskStore",
    "ToolResultPersistenceStore",
    "ToolResultStore",
]
