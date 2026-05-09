"""Run 领域模型及状态枚举定义。"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, model_validator

from app.core.models.error import ErrorCode


class RunStatus(str, Enum):
    """表示一次运行（Run）的生命周期状态。

    使用 str + Enum 的组合，保证状态值在序列化时自动变为可读字符串。
    """

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunType(str, Enum):
    """表示 Run 的执行类型。"""

    MASTER = "master"
    CHILD = "child"


class ExecutionMode(str, Enum):
    """表示 Run 的执行模式。"""

    FOREGROUND = "foreground"
    BACKGROUND = "background"


class Run(BaseModel):
    """表示一次在 Session 内的具体运行请求。

    Run 跟踪从请求发起到结果生成的完整生命周期，并记录可能的错误信息。
    """

    run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    status: RunStatus
    agent_id: str | None = None
    run_type: RunType = RunType.MASTER
    parent_run_id: str | None = None
    child_id: str | None = None
    tool_call_id: str | None = None
    execution_mode: ExecutionMode = ExecutionMode.FOREGROUND
    created_at: datetime
    # 为了兼容旧调用方，允许缺省后回填为 created_at
    updated_at: datetime | None = None
    metadata: Optional[dict] = None
    finished_at: Optional[datetime] = None
    output: Optional[str] = None
    error_code: Optional[ErrorCode] = None
    error_message: Optional[str] = None

    # 使用 model_validator(mode="after") 的原因是：
    # 1. 这里要同时检查 status、finished_at、output、error_code、error_message
    #    这些字段之间的组合关系，属于“跨字段一致性校验”。
    # 2. mode="after" 表示先让 Pydantic 完成单字段解析和类型转换，再把完整的
    #    Run 实例交给这里校验。这样这里看到的 self.status 一定已经是 RunStatus，
    #    self.finished_at 也已经是 datetime | None，避免手动处理原始输入值。
    # 3. 如果改用 field_validator，只适合校验单个字段；而这里的规则依赖多个字段
    #    的联合判断，因此放在模型级 after 校验里最直接、最稳妥。
    @model_validator(mode="after")
    def validate_state_consistency(self) -> Run:
        """校验 Run 状态与终态字段之间的一致性。

        该校验只约束当前任务中已经明确使用到的 RUNNING、COMPLETED、FAILED
        三种状态，避免服务层、存储层接受明显不可能的状态组合。
        """
        # 为了兼容旧调用方，未显式提供 updated_at 时默认回填为 created_at，
        # 这样存储层可以统一拿到稳定的更新时间字段，而不必要求所有调用点同步修改。
        if self.updated_at is None:
            self.updated_at = self.created_at

        # master run 不应携带 child 专属关系字段，避免把单层派发语义写乱。
        if self.run_type == RunType.MASTER:
            if self.parent_run_id is not None:
                raise ValueError("master run 不能携带 parent_run_id")
            if self.child_id is not None:
                raise ValueError("master run 不能携带 child_id")
            if self.tool_call_id is not None:
                raise ValueError("master run 不能携带 tool_call_id")
        # child run 必须带齐单层派发关系中的最小字段集合。
        if self.run_type == RunType.CHILD:
            if self.parent_run_id is None:
                raise ValueError("child run 必须包含 parent_run_id")
            if self.child_id is None:
                raise ValueError("child run 必须包含 child_id")
            if self.tool_call_id is None:
                raise ValueError("child run 必须包含 tool_call_id")

        # RUNNING 表示运行尚未结束，因此所有终态字段都必须保持为空
        if self.status == RunStatus.RUNNING:
            # 只要任一终态字段非空，就说明状态与字段组合已经冲突
            if any(
                value is not None
                for value in (
                    self.finished_at,
                    self.output,
                    self.error_code,
                    self.error_message,
                )
            ):
                # 抛出值错误，让 Pydantic 统一包装为 ValidationError
                raise ValueError("running 状态不能携带任何终态字段")
            # running 状态校验通过后，直接返回当前实例
            return self

        # COMPLETED 表示运行已成功结束，因此必须有结束时间和最终输出
        if self.status == RunStatus.COMPLETED:
            # 缺少结束时间时，无法证明该运行已进入终态
            if self.finished_at is None:
                raise ValueError("completed 状态必须包含 finished_at")
            # 缺少最终输出时，completed 状态语义不完整
            if self.output is None:
                raise ValueError("completed 状态必须包含 output")
            # 成功完成的运行不应再携带错误信息
            if self.error_code is not None or self.error_message is not None:
                raise ValueError("completed 状态不能携带错误字段")
            # completed 状态校验通过后，直接返回当前实例
            return self

        # FAILED 表示运行因错误结束，因此必须有结束时间和完整错误信息
        if self.status == RunStatus.FAILED:
            # 缺少结束时间时，失败终态同样不完整
            if self.finished_at is None:
                raise ValueError("failed 状态必须包含 finished_at")
            # 缺少错误码时，下游无法做程序化错误处理
            if self.error_code is None:
                raise ValueError("failed 状态必须包含 error_code")
            # 缺少错误描述时，日志与接口返回都无法给出有效上下文
            if self.error_message is None:
                raise ValueError("failed 状态必须包含 error_message")
            # 失败终态不应再携带成功输出
            if self.output is not None:
                raise ValueError("failed 状态不能携带 output")

        # CANCELLED 表示运行被外部取消，约束与 FAILED 类似：必须有 finished_at、error_code、error_message，不能有 output
        if self.status == RunStatus.CANCELLED:
            if self.finished_at is None:
                raise ValueError("cancelled 状态必须包含 finished_at")
            if self.error_code is None:
                raise ValueError("cancelled 状态必须包含 error_code")
            if self.error_message is None:
                raise ValueError("cancelled 状态必须包含 error_message")
            if self.output is not None:
                raise ValueError("cancelled 状态不能携带 output")

        # 对当前任务未明确约束的其他状态，先保持现有兼容行为
        return self
