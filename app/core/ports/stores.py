"""核心存储端口定义。

该模块只描述业务层需要的持久化能力，不导入 Redis、MongoDB、PostgreSQL
或任何具体存储 SDK。企业替换存储时应实现这些 Protocol。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.core.models.error import ErrorCode
from app.core.models.run import Run, RunStatus
from app.core.models.session import Session
from app.core.models.stored_message import StoredMessage
from app.core.models.task import TaskItem


@dataclass(slots=True)
class ContextSummaryState:
    """会话最近一次上下文摘要的边界状态。

    上下文摘要压缩后产生一条摘要消息和一个新的活动开始偏移，该 dataclass
    就是为了完整记录这个"摘要边界"，以便后续读取活动上下文时可以准确地
    从摘要之后起始。如果不记录这个状态，每次读取上下文时都要重新扫描全部
    历史消息来判断哪些已摘要、哪些还是活动的，效率会很低。
    """

    summary_message_id: str
    active_start_message_id: str | None
    summary_offset: int | None = None
    active_start_offset: int | None = None


@dataclass(frozen=True, slots=True)
class SessionChildSummary:
    """表示当前 session 下一个可恢复子代理的最新摘要。

    当子代理任务因某种原因被打断（如达到 max_turns 或用户发送新消息）时，
    需要保留一个足够恢复的摘要，这样下次用户请求同一个子代理时可以不重头
    开始。因为该摘要只用于列表展示和后续恢复决策，所以设计为不可变 frozen，
    避免业务层意外篡改摘要内容。
    """

    resume_id: str
    subagent_type: str
    description: str


@dataclass(slots=True)
class PersistedToolResult:
    """单条已持久化工具结果记录。

    某些工具的输出非常大（如完整的代码分析报告），需要通过 Hook 单独存储到
    独立 key 中，避免把超大内容塞入上下文消息体。该 dataclass 就是存储层
    返回给查询方的结果视图，包含完整的内容文本和元数据。
    """

    key: str
    session_id: str
    tool_name: str
    content: str
    created_at: datetime
    content_length: int


class SessionStore(Protocol):
    """会话、上下文和子代理摘要存储端口。

    一个 SessionStore 负责所有与 Session 相关的读写操作，包括：
    - 会话元数据的创建与查询
    - 主会话和 child 的上下文消息写入与读取
    - 上下文摘要边界状态的管理
    - 子代理可恢复摘要的索引与更新
    - 会话资源的级联删除

    采用 Protocol 而非 ABC 的原因是：Protocol 支持结构化子类型，
    实现方不需要显式继承，只要方法签名匹配即可被接受为有效的 SessionStore。
    这符合依赖倒置原则——业务层依赖抽象（Protocol），而实现方只需满足
    这个接口约定即可。
    """

    async def create_session(self, session: Session) -> Session:
        """创建会话并返回创建后的会话模型。

        存储层需要在创建时补充或回填字段（如 created_at），
        所以直接返回完整 Session 实例而非 None。
        """

    async def get_session(self, session_id: str) -> Session | None:
        """按 session_id 查询会话。

        会话不存在时返回 None 而非抛异常，因为"会话不存在"是一种
        正常查询结果而非异常情况，调用方应通过返回值判断。
        """

    async def add_session_run(
        self,
        session_id: str,
        run_id: str,
        created_at: datetime | None = None,
        created_at_ts: float | None = None,
    ) -> None:
        """把 run_id 写入 session 级运行索引。

        created_at 和 created_at_ts 两个参数是为了兼容不同存储后端：
        - datetime 对象适合 MongoDB、PostgreSQL 等原生支持日期类型的数据库
        - timestamp float 适合 Redis 等需要按分数排序的存储
        实现方可根据后端类型选择使用哪一个，不必同时支持两者。
        """

    async def list_session_runs(self, session_id: str) -> list[str]:
        """列出 session 下的 run_id。"""

    async def ensure_session_child_registered(self, session_id: str, child_id: str) -> None:
        """确保 child 已出现在 session 级索引中，不覆盖已有摘要。

        该方法是幂等的：如果 child 索引已存在则不进行任何操作。
        索引的存在不等同于摘要的完整——后续仍需要通过 upsert 写入摘要。
        这种分离设计是因为注册索引和写入摘要在业务时序上可能不同步。
        """

    async def list_session_children(self, session_id: str) -> list[str]:
        """列出 session 下的 child_id。"""

    async def upsert_session_child_summary(
        self,
        session_id: str,
        child_id: str,
        subagent_type: str,
        description: str,
    ) -> None:
        """写入或覆盖 child 的可恢复摘要。

        使用 upsert 语义（不存在则插入，存在则更新）是因为：
        1. 子代理运行过程中可能多次更新摘要，每次都是全覆盖
        2. 不需要先查询再判断是 insert 还是 update——减少一次网络往返
        """

    async def list_session_child_summaries(self, session_id: str) -> list[SessionChildSummary]:
        """列出 session 下全部可恢复 child 摘要。"""

    async def get_main_message_count(self, session_id: str) -> int:
        """获取主会话上下文消息数。

        用于快速判断上下文是否需要摘要压缩，避免每次都要拉取全部消息。
        """

    async def append_main_message(
        self,
        session_id: str,
        message: StoredMessage,
        source_run_id: str | None = None,
        child_id: str | None = None,
    ) -> None:
        """向主会话上下文追加消息。

        source_run_id 用于在消息级别记录消息来源于哪次 Run，
        child_id 则标记该消息是否由某个子代理产生——即便是在主会话上下文中。
        这两个可选参数让存储层可以在追加时自动回填消息的元数据字段，
        避免调用方在追加前手动处理 StoredMessage.meta。
        """

    async def list_main_messages(self, session_id: str, start: int = 0, end: int = -1) -> list[StoredMessage]:
        """读取主会话上下文消息。

        start 和 end 参数支持按范围读取，避免一次性拉取过大的上下文。
        end=-1 表示读取到末尾，与 Python list slice 语义一致。
        """

    async def list_main_active_messages(self, session_id: str) -> list[StoredMessage]:
        """读取主会话当前活动上下文消息。

        活动上下文是指从最近一次摘要边界之后到当前的消息范围。
        实现方应在内部查询 ContextSummaryState 状态并据此截取。
        """

    async def list_main_active_messages_with_indices(self, session_id: str) -> tuple[list[StoredMessage], list[int]]:
        """读取主会话活动上下文消息及其原始偏移。

        与 list_main_active_messages 不同，该方法同时返回消息列表和
        对应的原始偏移索引，用于摘要规划时需要知道哪些位置的消息正在
        被压缩、哪些位置是新的活动消息。
        """

    async def get_main_context_summary_state(self, session_id: str) -> ContextSummaryState | None:
        """读取主会话摘要边界状态。

        返回 None 表示从未进行过摘要压缩（即整个上下文都是活动的）。
        """

    async def append_main_context_summary(
        self,
        session_id: str,
        message: StoredMessage,
        active_start_message: StoredMessage | None,
        active_start_offset: int | None = None,
    ) -> ContextSummaryState:
        """向主会话写入摘要消息并更新摘要边界状态。

        该方法会原子地完成两件事：
        1. 将摘要消息追加到消息列表中
        2. 更新 ContextSummaryState 以反映新的摘要边界
        通过一次调用来保证这两个操作的原子性，避免中间状态不一致。
        返回新的 ContextSummaryState 以便调用方确认边界。
        """

    async def mark_main_history_dirty(self, session_id: str) -> None:
        """标记主会话历史需要后续修复。

        当消息编排或删除操作导致消息序号出现空洞时设置此标记，
        后续的修复流程会扫到这个标记并执行修复。而标记本身的存储
        应该足够轻量，通常是一个单独的短生命周期 key 或布尔字段。
        """

    async def is_main_history_dirty(self, session_id: str) -> bool:
        """查询主会话历史 dirty 标记。"""

    async def get_child_message_count(self, session_id: str, child_id: str) -> int:
        """获取 child 上下文消息数。"""

    async def append_child_message(
        self,
        session_id: str,
        child_id: str,
        message: StoredMessage,
        source_run_id: str | None = None,
        subagent_type: str | None = None,
    ) -> None:
        """向 child 上下文追加消息。"""

    async def list_child_messages(
        self,
        session_id: str,
        child_id: str,
        start: int = 0,
        end: int = -1,
    ) -> list[StoredMessage]:
        """读取 child 上下文消息。"""

    async def list_child_context_messages(
        self,
        session_id: str,
        child_id: str,
        start: int = 0,
        end: int = -1,
    ) -> list[StoredMessage]:
        """读取 child 上下文消息，保留当前命名兼容入口。

        与 list_child_messages 语义一致，只是命名不同。
        保留这个副本是为了兼容现有调用方，新实现只需实现 list_child_messages。
        """

    async def list_child_active_messages(self, session_id: str, child_id: str) -> list[StoredMessage]:
        """读取 child 当前活动上下文消息。"""

    async def list_child_active_messages_with_indices(
        self,
        session_id: str,
        child_id: str,
    ) -> tuple[list[StoredMessage], list[int]]:
        """读取 child 活动上下文消息及其原始偏移。"""

    async def get_child_context_summary_state(self, session_id: str, child_id: str) -> ContextSummaryState | None:
        """读取 child 摘要边界状态。"""

    async def append_child_context_summary(
        self,
        session_id: str,
        child_id: str,
        message: StoredMessage,
        active_start_message: StoredMessage | None,
        active_start_offset: int | None = None,
    ) -> ContextSummaryState:
        """向 child 写入摘要消息并更新摘要边界状态。"""

    async def mark_child_history_dirty(self, session_id: str, child_id: str) -> None:
        """标记 child 历史需要后续修复。"""

    async def is_child_history_dirty(self, session_id: str, child_id: str) -> bool:
        """查询 child 历史 dirty 标记。"""

    async def delete_session_main_context(self, session_id: str) -> int:
        """删除主会话上下文相关数据。

        返回删除的消息条目数，便于日志和监控。
        """

    async def delete_child_context(self, session_id: str, child_id: str) -> int:
        """删除指定 child 上下文相关数据。"""

    async def delete_session_metadata_and_indices(self, session_id: str) -> int:
        """删除会话元数据和 session 级索引。

        该方法与 delete_session_main_context 分开设计的原因是：
        有些场景只需要删除上下文（如重置会话），而不需要删除元数据。
        级联删除时应先调 delete_session_main_context，再调
        delete_child_context（遍历所有 child），最后调此方法。
        """


class RunStore(Protocol):
    """Run 存储端口。

    Run 是会话内单次请求的执行记录，包含运行状态、起止时间和错误信息。
    该端口负责 Run 的 CRUD，尤其需要关注状态一致性（运行中不能有终态字段）。
    Push 模型下 Run 的创建和终态更新是两个独立操作，存储层不应原子化绑定。
    """

    async def create_run(self, run: Run, ttl_seconds: int | None = None) -> None:
        """创建 Run 记录。

        ttl_seconds 参数支持某些存储后端（如 Redis）为记录设置自动过期时间。
        对于永不过期的后端（如 PostgreSQL），实现方可忽略此参数。
        """

    async def get_run(self, run_id: str) -> Run | None:
        """按 run_id 查询 Run。

        返回 None 表示该 run_id 不存在。
        """

    async def update_run(self, run: Run) -> None:
        """覆盖更新完整 Run。

        这是一个全量更新操作，传入完整的 Run 对象并覆盖存储中的已有记录。
        适用于需要同时更新多个字段的场景。如果只需要更新终态字段，
        使用 update_run_fields 会更精确。
        """

    async def update_run_fields(
        self,
        run_id: str,
        status: RunStatus,
        finished_at: datetime,
        output: str | None = None,
        error_code: ErrorCode | None = None,
        error_message: str | None = None,
    ) -> None:
        """更新 Run 终态字段。

        该方法与 update_run 的区别在于：
        - 只更新终态相关字段，不接触其他字段，减少数据竞争风险
        - 参数提平化，调用方不需要自己构造完整的 Run 对象
        - 与 run model 中的校验逻辑解耦：存储层只负责写入，状态一致性
          应该由业务层或 Run model 自身的校验器来保证

        之所以这样设计，是因为终态更新是最高频的写操作，简化该操作
        可以减少调用方出错的概率。
        """

    async def delete_run(self, run_id: str) -> int:
        """删除单个 Run。"""

    async def delete_runs(self, run_ids: list[str]) -> int:
        """批量删除 Run。"""


class TaskStore(Protocol):
    """Task 存储端口。

    Task 是会话内的任务跟踪项（如子代理要执行的步骤清单）。
    与 Run 不同，Task 是业务级别的持久化对象，需要支持按 session 范围查询。
    """

    async def next_task_id(self, session_id: str) -> str:
        """生成 session 内下一个 Task ID。

        该方法设计为存储层方法而不是自增 ID 生成器，是因为：
        1. 需要保证生成的 ID 在 session 内唯一
        2. 不同 session 的 ID 生成器互不影响
        3. 存储层最清楚如何高效完成这个计数操作
        """

    async def create_task(self, session_id: str, task: TaskItem) -> None:
        """创建 Task。"""

    async def get_task(self, session_id: str, task_id: str) -> TaskItem | None:
        """读取 Task。"""

    async def list_tasks(self, session_id: str) -> list[TaskItem]:
        """列出 session 下的 Task。"""

    async def save_task(self, session_id: str, task: TaskItem) -> None:
        """保存 Task。

        使用 save 语义（create or update）而不是区分 insert/update，
        因为调用方无法确定该 Task 是新建还是已存在——统一操作更简单。
        """

    async def delete_task(self, session_id: str, task_id: str) -> bool:
        """删除 Task。"""


class LockStore(Protocol):
    """会话单活跃运行锁端口。

    保证同一时间一个 session 内最多只有一个活跃 Run 在执行。
    这是实现"会话级串行化"的锁机制——用户发新消息时如果上一轮还没跑完，
    要么拒绝要么等待。采用独立的 LockStore 而非直接让 RunStore 管理锁
    的原因是：锁的语义和生命周期与 Run 不同——锁需要心跳续期，Run 不需要。
    把锁剥离出来可以让两种存储实现各自独立演进和独立测试。
    """

    async def acquire(self, session_id: str, run_id: str, ttl_seconds: int) -> bool:
        """尝试获取 session 运行锁。

        返回 True 表示成功获取锁（当前无其他活跃 Run），
        False 表示锁已被其他 Run 持有。
        """

    async def get_active_run_id(self, session_id: str) -> str | None:
        """查询 session 当前活跃 run_id。

        返回 None 表示没有活跃 Run（锁已被释放或过期）。
        """

    async def extend(self, session_id: str, run_id: str, ttl_seconds: int) -> bool:
        """续期 session 运行锁。

        心跳组件需要定期调用此方法来防止锁因 TTL 过期而自动释放。
        如果返回 False 说明续期失败（可能锁已被其他进程抢走），
        此时业务层应主动终止当前 Run。
        """

    async def release(self, session_id: str, run_id: str) -> bool:
        """释放 session 运行锁。

        返回 True 表示释放成功。如果锁已经过期或被其他 Run 持有，
        返回 False 也是合理的结果。
        """


class ToolResultPersistenceStore(Protocol):
    """大工具结果写入端口，供 Hook 使用。

    当工具的输出内容超出上下文窗口可容纳的大小时，Hook 会将完整输出
    持久化到独立存储中，并在上下文中仅保留一个引用指针。
    """

    async def persist_result(self, session_id: str, tool_name: str, content: str) -> str:
        """持久化完整工具结果并返回可查询 key。"""


class ToolResultStore(ToolResultPersistenceStore, Protocol):
    """大工具结果完整存储端口。

    继承 ToolResultPersistenceStore 以保证写入能力的一致性，
    同时扩展了读取、批量删除和 key 命名空间判断的能力。
    """

    async def get_result(self, key: str, session_id: str) -> PersistedToolResult | None:
        """按 key 读取当前 session 的工具结果。

        增加 session_id 参数作为额外的安全校验——即使 key 被猜出，
        也要保证只能访问同 session 内的数据，实现租户隔离。
        """

    async def delete_session_results(self, session_id: str) -> int:
        """删除 session 下的工具结果。"""

    def is_tool_result_key(self, value: str) -> bool:
        """判断字符串是否属于当前工具结果 key 命名空间。

        用于在级联删除时区分哪些 key 是工具结果、哪些是其他数据。
        这是一个同步方法，因为它只是格式检查，不需要 IO。
        """
