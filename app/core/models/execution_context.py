"""执行上下文模型定义。

定义 ExecutionContext 数据类，封装工具执行时需要的所有上下文信息。
"""

from __future__ import annotations  # 启用未来注解，避免前向引用问题

import asyncio  # 导入 asyncio 模块，用于异步事件
from dataclasses import dataclass, field  # 导入数据类装饰器和字段函数


@dataclass
class ExecutionContext:
    """工具执行上下文。

    封装工具执行时需要的所有上下文信息，包括运行标识、会话标识、
    请求元数据、Agent 配置和取消事件。该上下文在 AgentLoop 中构造，
    传递给 Tool.call() 方法、Hook 和 Runtime，使各层可以访问执行环境信息。

    Attributes:
        run_id: 运行的唯一标识符，用于追踪单次执行
        session_id: 会话的唯一标识符，用于关联对话上下文
        metadata: 请求元数据字典，可包含权限信息、业务上下文等
        agent: Agent 配置对象，包含模型、系统提示等信息
        cancel_event: 异步取消事件，用于外部中断当前运行
        run_type: 运行类型，区分 master 主运行和 child 子运行，默认 master
        child_id: 子代理会话内稳定标识，用于 plan/task 隔离命名空间
        tool_call_id: 当前工具调用的唯一 ID，在 for_tool_call() 派生时设置
        tool_name: 当前工具的名称，在 for_tool_call() 派生时设置
    """

    # 运行唯一标识符，用于追踪单次执行
    run_id: str

    # 会话唯一标识符，用于关联对话上下文
    session_id: str

    # 请求元数据字典，可包含权限信息、业务上下文等
    # 为 None 时表示请求未提供元数据
    metadata: dict | None

    # Agent 配置对象，包含模型、系统提示等信息
    agent: "Agent"  # type: ignore  # 使用字符串前向引用避免循环导入

    # 异步取消事件，用于外部中断当前运行
    # 默认构造一个未触发的 Event，各层可通过 is_set() 检查是否被取消
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)

    # 运行类型，用于区分主运行（master）和子代理运行（child），默认为 master
    run_type: str = "master"

    # 子代理的会话内稳定标识符。仅当 run_type="child" 时设置，
    # 主代理上下文保持 None，用于 plan/task 隔离命名空间推导。
    child_id: str | None = None

    # 当前工具调用的唯一标识符，None 表示不在工具调用上下文中
    tool_call_id: str | None = None

    # 当前工具的名称，None 表示不在工具调用上下文中
    tool_name: str | None = None

    def for_tool_call(self, *, tool_call_id: str, tool_name: str) -> "ExecutionContext":
        """为单个工具调用派生上下文，避免工具从入参猜测调用 ID。

        基于当前上下文创建一个新的 ExecutionContext，其中 tool_call_id 和
        tool_name 被设置为指定值，其余字段保持不变。这确保了工具在执行时
        可以准确获知自己被调用的上下文，而无需从输入参数中推断。

        Args:
            tool_call_id: 当前工具调用的唯一标识符
            tool_name: 当前被调用的工具名称

        Returns:
            一个新的 ExecutionContext 实例，tool_call_id 和 tool_name 已更新
        """
        return ExecutionContext(
            run_id=self.run_id,
            session_id=self.session_id,
            metadata=self.metadata,
            agent=self.agent,
            cancel_event=self.cancel_event,
            run_type=self.run_type,
            child_id=self.child_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
        )

    def resolve_plan_session_id(self) -> str:
        """解析 plan/task 存储使用的隔离 session ID。

        子代理调用（run_type="child" 且 child_id 非空）时返回
        "{session_id}:child:{child_id}" 组合键，使不同子代理的
        task 数据在 Redis 中物理隔离。主代理调用时返回原始 session_id。

        Returns:
            用于 plan/task 存储的隔离 session ID 字符串。
        """
        if self.run_type == "child" and self.child_id:
            return f"{self.session_id}:child:{self.child_id}"
        return self.session_id
