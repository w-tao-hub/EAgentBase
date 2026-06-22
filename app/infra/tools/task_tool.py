"""主代理派发子代理的 Task 工具。

TaskTool 是 master agent 的工具，用于启动一个同步 child agent 执行子任务。
child agent 在独立的 child context 中运行，执行完成后将结果返回给 master。

约束：
- 只有 master run 可以调用 Task 工具（child 递归调用会被拒绝）
- subagent_type 匹配大小写敏感（"plan" 不会命中 "Plan"）
- resume 参数允许恢复已有的 child 上下文继续执行
"""

from __future__ import annotations

import re
import uuid

from app.core.models.agent import AgentExecutionProfile
from app.core.models.error import ErrorCode
from app.core.models.execution_context import ExecutionContext
from app.core.models.tool import Tool, ToolResult, ToolResultMeta


class TaskTool(Tool):
    """启动一个同步 child agent 执行子任务。

    该工具只能被 master agent 在 master run 上下文中调用。
    child agent 不能再次调用 Task 派发子代理（防止递归）。

    使用方式：
    - 首次派发：提供 description、prompt、subagent_type
    - 恢复执行：额外提供 resume 参数指向已有的 child_id
    """

    def __init__(
        self,
        child_runner,
        child_profiles: dict[str, AgentExecutionProfile] | None = None,
    ) -> None:
        """初始化 TaskTool。

        Args:
            child_runner: ChildAgentRunner 实例，负责实际执行 child agent
            child_profiles: 子代理 profile 字典，key 为子代理类型名称，用于动态构建工具描述
        """
        self._child_runner = child_runner  # 保存 child runner 引用
        self._child_profiles = child_profiles or {}  # 保存子代理 profile 字典

    @property
    def name(self) -> str:
        """工具名称，LLM 通过该名称调用工具。"""
        return "Task"

    @property
    def description(self) -> str:
        """工具描述，供 LLM 了解工具用途。

        动态生成：当存在子代理时，列出所有可用代理类型的名称、描述及可使用的工具列表；
        无子代理时返回基础提示。
        """
        if not self._child_profiles:
            return (
                "启动一个子代理，自主处理复杂的多步骤任务。\n\n"
                "当前无可用代理。"
            )

        lines: list[str] = [
            "启动一个新代理，自主处理复杂的多步骤任务。",
            "",
            "任务工具会启动专用代理（子进程），自主处理复杂任务。每种代理类型都具备特定能力和可用工具。",
            "",
            "可用代理类型及其可使用的工具：",
        ]

        for name in sorted(self._child_profiles.keys()):
            profile = self._child_profiles[name]
            desc = profile.agent.description or ""
            tool_names = profile.tool_registry.list_tools()
            if tool_names:
                tools_str = "、".join(tool_names)
                lines.append(f"- {name}：{desc}（工具：{tools_str}）")
            elif name == "Worker":
                lines.append(f"- {name}：{desc}（工具由主代理动态指定）**优先使用其他可以满足需求的代理，若没有能满足的代理再使用 {name} 代理**")
            else:
                lines.append(f"- {name}：{desc}（无工具）")

        lines.extend([
            "",
            "使用任务工具时，必须指定 subagent_type 参数以选择代理类型。",
        ])

        return "\n".join(lines)

    @property
    def input_schema(self) -> dict:
        """Task 工具输入参数的 JSON Schema 定义。

        动态构建：subagent_type 的描述会根据 _child_profiles 动态提示当前可用的代理类型。

        必填参数：
        - description: 简短任务描述（3-5 词）
        - prompt: 代理要执行的具体任务内容
        - subagent_type: 子代理类型名称（大小写敏感）

        可选参数：
        - resume: 要恢复执行的子代理 child_id
        """
        # 构建可用代理类型提示
        if self._child_profiles:
            available = "、".join(sorted(self._child_profiles.keys()))
            subagent_desc = f"子代理类型名称（当前可用：{available}）"
        else:
            subagent_desc = "子代理类型名称（当前无可用代理）"

        return {
            "type": "object",  # 参数类型为对象
            "properties": {  # 参数属性定义
                "description": {
                    "description": "简短（3-5 词）的任务描述",  # 参数用途
                    "type": "string",  # 字符串类型
                },
                "prompt": {
                    "description": "代理要执行的任务",  # 参数用途
                    "type": "string",  # 字符串类型
                },
                "subagent_type": {
                    "description": subagent_desc,  # 参数用途，动态提示可用类型
                    "type": "string",  # 字符串类型
                },
                "resume": {
                    "description": "可选，要恢复的子代理 ID",  # 参数用途
                    "type": "string",  # 字符串类型
                },
                "tools": {
                    "description": "可选，仅对提示中标注「工具由主代理动态指定」的代理类型生效，用于指定子代理可用工具名称列表",  # 参数用途
                    "type": "array",  # 数组类型
                    "items": {"type": "string"},  # 元素为字符串
                },
            },
            "required": ["description", "prompt", "subagent_type"],  # 必填字段列表
            "additionalProperties": False,  # 不允许额外参数
        }

    async def call(self, input: dict, context: ExecutionContext) -> ToolResult:
        """派发子代理并把最终输出作为工具结果返回给 master。

        执行流程：
        1. 校验调用上下文（必须是 master run，必须有 tool_call_id）
        2. 解析和校验入参（subagent_type、prompt）
        3. 解析 child_id 和 is_resume
        4. 调用 child_runner.run_child 执行子代理
        5. 返回包含 child 输出的 ToolResult

        Args:
            input: 工具输入参数，必须包含 description、subagent_type 和 prompt
            context: 执行上下文，包含 run_id、session_id、run_type 等

        Returns:
            ToolResult: 成功时包含 child 输出，失败时 is_error=True
        """
        # 安全检查：只有 master run 可以派发子代理，防止递归
        if context.run_type != "master":  # 检查运行类型
            return ToolResult(  # 返回错误结果
                content=f"{ErrorCode.CHILD_AGENT_RECURSION_FORBIDDEN.value}: child 不能再次调用 Task",  # 错误消息
                is_error=True,  # 标记为错误
            )
        if context.tool_call_id is None:  # 检查 tool_call_id 是否存在
            return ToolResult(content="Task 缺少父级 tool_call_id", is_error=True)  # 返回错误

        # 解析入参并去除首尾空白
        subagent_type = str(input.get("subagent_type", "")).strip()  # 子代理类型
        prompt = str(input.get("prompt", "")).strip()  # 任务 prompt
        description = str(input.get("description", "")).strip()  # 任务描述
        resume = str(input.get("resume", "")).strip()  # 可选 resume 参数
        tools_raw = input.get("tools", None)  # 可选 tools 参数
        tool_names = (
            tuple(str(t).strip() for t in tools_raw if isinstance(t, str))
            if isinstance(tools_raw, list)
            else None
        )  # 归一化为 tuple 或 None
        if not description or not subagent_type or not prompt:  # 必填参数缺失
            return ToolResult(content="Task 缺少 description、subagent_type 或 prompt", is_error=True)  # 返回错误

        # 校验 subagent_type 是否挂载到当前主代理
        # TaskTool 只持有当前主代理可见的子代理 profiles
        if subagent_type not in self._child_profiles:  # 子代理类型未挂载到当前主代理
            return ToolResult(  # 返回错误结果
                content=f"{ErrorCode.SUBAGENT_NOT_MOUNTED.value}: {subagent_type} 未挂载到当前主代理",  # 错误消息
                is_error=True,  # 标记为错误
            )

        # subagent_type 匹配大小写敏感："plan" 不会命中 "Plan"
        child_id = resume or self._new_child_id(subagent_type)  # 有 resume 时用现有 child_id，否则生成新的
        is_resume = bool(resume)  # 有 resume 值即为 resume 模式
        try:
            result = await self._child_runner.run_child(  # 委托给 ChildAgentRunner
                session_id=context.session_id,  # 所属会话
                parent_run_id=context.run_id,  # 父（master）run ID
                tool_call_id=context.tool_call_id,  # 触发工具调用 ID
                subagent_type=subagent_type,  # 子代理类型
                child_id=child_id,  # child 稳定标识
                prompt=prompt,  # 任务 prompt
                description=description,  # 任务描述
                metadata=context.metadata,  # 请求元数据
                cancel_event=context.cancel_event,  # 外部取消事件
                is_resume=is_resume,  # 是否为 resume 模式
                tool_names=tool_names,  # 动态工具列表
            )
        except ValueError as error:  # 捕获子代理执行中的业务异常
            return ToolResult(content=str(error), is_error=True)  # 将异常消息作为工具错误返回

        return ToolResult(  # 返回成功的工具结果
            content=(  # 包含 child 执行摘要
                f"子代理 {subagent_type} 已完成。\n"  # 子代理类型标识
                f"child_id: {result.child_id}\n"  # child 稳定标识
                f"child_run_id: {result.child_run_id}\n"  # child run ID
                f"输出:\n{result.output}"  # child 最终输出
            ),
            meta=ToolResultMeta(task_child_id=result.child_id),
        )

    @staticmethod
    def _new_child_id(subagent_type: str) -> str:
        """生成带子代理类型前缀的稳定 child id。

        规则：
        1. 将 subagent_type 中的非字母数字字符替换为 "-"
        2. 去除首尾连字符
        3. 转为小写作为前缀
        4. 追加 12 位 UUID hex 作为唯一后缀

        例如："Plan" -> "plan-a1b2c3d4e5f6"

        Args:
            subagent_type: 子代理类型名称

        Returns:
            str: 格式为 "{prefix}-{12位hex}" 的 child_id
        """
        prefix = re.sub(r"[^a-zA-Z0-9_-]+", "-", subagent_type).strip("-").lower() or "child"  # 清理前缀，兜底为 "child"
        return f"{prefix}-{uuid.uuid4().hex[:12]}"  # 拼接前缀和后缀
