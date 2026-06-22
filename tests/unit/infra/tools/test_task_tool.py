"""Task 派发工具测试。

测试 TaskTool 的入参校验、resume 参数透传、大小写敏感的 subagent_type 匹配
以及 child 递归调用禁止等核心逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from app.core.models.error import ErrorCode
from app.core.models.execution_context import ExecutionContext
from app.infra.tools.task_tool import TaskTool
from tests.fakes import create_fake_agent


@dataclass  # 使用数据类封装 child 执行结果
class FakeChildResult:
    """TaskTool 测试用 child 执行结果。

    模拟 ChildAgentRunner.run_child() 的返回值，使测试无需真实执行 child agent。
    """

    child_id: str  # child 的唯一标识符
    child_run_id: str  # child 的 run 唯一标识符
    output: str  # child 执行的最终输出


class FakeChildRunner:
    """记录 TaskTool 调用参数的 fake child runner。

    不执行真实的 child agent 流程，只记录调用参数并返回预设结果，
    用于验证 TaskTool 是否正确收集和传递参数。
    支持 known_subagent_types 参数模拟真实 ChildAgentRunner 的大小写敏感 profile 查找行为。
    """

    def __init__(self, known_subagent_types: set[str] | None = None) -> None:
        """初始化 fake runner 的状态。

        Args:
            known_subagent_types: 已知的 subagent_type 集合，用于模拟 profile 查找。
                                  传入 None 表示不限制（默认行为）。
        """
        self.last_call: dict | None = None  # 记录最近一次 run_child 的调用参数
        self.raise_on_resume: bool = False  # 设为 True 时模拟 resume 不存在 child 上下文的错误
        self._known_types = known_subagent_types  # 已知的子代理类型集合

    async def run_child(self, **kwargs) -> FakeChildResult:
        """模拟 run_child 方法，记录参数并返回假结果。

        当 raise_on_resume 为 True 且传入了 is_resume=True 时，
        会抛出 ValueError 模拟 CHILD_AGENT_CONTEXT_INVALID 错误。

        当 known_subagent_types 已设置且 subagent_type 不在其中时，
        会抛出 ValueError 模拟 UNKNOWN_SUBAGENT 错误（模拟大小写敏感匹配）。

        Args:
            **kwargs: 透传所有 run_child 参数

        Returns:
            FakeChildResult: 包含 child_id、child_run_id 和 output 的假结果

        Raises:
            ValueError: 当 raise_on_resume=True 或 subagent_type 不在已知类型中时
        """
        self.last_call = kwargs  # 记录本次调用的全部参数
        if self.raise_on_resume and kwargs.get("is_resume"):  # 配置了抛出模式且确实为 resume 调用
            raise ValueError(f"{ErrorCode.CHILD_AGENT_CONTEXT_INVALID.value}: child 上下文不存在: {kwargs.get('child_id', '')}")
        # 模拟真实 ChildAgentRunner 的大小写敏感 profile 查找
        if self._known_types is not None:  # 配置了已知类型集合
            subagent_type = kwargs.get("subagent_type", "")  # 获取请求的子代理类型
            if subagent_type not in self._known_types:  # 不在已知集合中（大小写敏感）
                raise ValueError(f"{ErrorCode.UNKNOWN_SUBAGENT.value}: {subagent_type}")  # 模拟 UNKNOWN_SUBAGENT 错误
        return FakeChildResult(  # 返回预设的假执行结果
            child_id=kwargs["child_id"],  # 原样返回传入的 child_id
            child_run_id="child-run-1",  # 假 run_id
            output="child output",  # 假输出内容
        )

    async def mark_result_backfilled(self, child_run_id: str) -> None:
        """模拟标记结果已回填，仅记录请求不做实际操作。

        Args:
            child_run_id: 需要标记回填的 child run ID
        """
        pass  # 测试中无需实际标记


@pytest.mark.asyncio  # 标记为异步测试
async def test_task_tool_case_sensitive_subagent_type() -> None:
    """测试 subagent_type 匹配大小写敏感。

    验证 "plan"（小写）不会命中 "Plan"。挂载校验层大小写敏感，
    child_profiles 中只有 "Plan" 时，传入 "plan" 应返回 SUBAGENT_NOT_MOUNTED 错误。

    真实 ChildAgentRunner 使用字典直接键查找 ("Plan" in profiles)，
    不进行 lower() 转换，因此 "plan" != "Plan" 会导致 KeyError。
    """
    runner = FakeChildRunner(known_subagent_types={"Plan"})  # 创建 fake runner，只认 "Plan"（大小写敏感）
    # TaskTool 传入包含 "Plan" 的 child_profiles，确保挂载校验通过
    mock_profile = MagicMock()  # 最小 mock profile，call() 仅检查 key 是否存在
    tool = TaskTool(runner, child_profiles={"Plan": mock_profile})  # 创建被测试的 TaskTool 实例
    context = ExecutionContext(  # 构造 master 执行上下文
        run_id="master-run",  # master run ID
        session_id="session-1",  # 会话 ID
        metadata=None,  # 无额外元数据
        agent=create_fake_agent(),  # 假 Agent 实例
        run_type="master",  # master 类型，满足非 child 检查
        tool_call_id="call-1",  # 合法的 tool_call_id
        tool_name="Task",  # 当前工具名
    )

    # "plan" (小写) 不应命中 "Plan"，挂载校验层按大小写敏感拒绝
    result = await tool.call(
        {"description": "制定计划", "prompt": "制定实施计划", "subagent_type": "plan"},
        context,
    )
    assert result.is_error is True  # 应该返回错误
    assert "SUBAGENT_NOT_MOUNTED" in result.content  # 确认错误码出现在结果中
    # 验证 runner 未被调用（挂载校验在进入 runner 之前就已拦截）
    assert runner.last_call is None  # 确认 runner 没有被调用


@pytest.mark.asyncio  # 标记为异步测试
async def test_task_tool_dispatches_child_with_resume() -> None:
    """测试 Task 工具会把 resume 作为 child_id 传给 runner。

    验证 resume 参数正确透传，且工具结果中包含 child_id 标识。
    """
    runner = FakeChildRunner()  # 创建 fake runner
    # TaskTool 传入包含 "Plan" 的 child_profiles，确保挂载校验通过
    mock_profile = MagicMock()  # 最小 mock profile
    tool = TaskTool(runner, child_profiles={"Plan": mock_profile})  # 创建被测试的 TaskTool 实例
    context = ExecutionContext(  # 构造 master 执行上下文
        run_id="master-run",  # master run ID
        session_id="session-1",  # 会话 ID
        metadata={"tenant": "t1"},  # 包含租户元数据
        agent=create_fake_agent(),  # 假 Agent 实例
        run_type="master",  # master 类型
        tool_call_id="call-1",  # 合法的 tool_call_id
        tool_name="Task",  # 当前工具名
    )

    result = await tool.call(  # 调用 Task 工具
        {
            "description": "制定计划",  # 任务描述
            "prompt": "请制定实施计划",  # 任务 prompt
            "subagent_type": "Plan",  # 子代理类型（大小写敏感）
            "resume": "plan-existing",  # 要恢复的 child_id
        },
        context,
    )

    assert result.is_error is False  # 应该成功完成
    assert "child_id: plan-existing" in result.content  # 确认输出包含正确的 child_id
    assert runner.last_call["child_id"] == "plan-existing"  # child_id 正确透传
    assert runner.last_call["tool_call_id"] == "call-1"  # tool_call_id 正确透传


@pytest.mark.asyncio  # 标记为异步测试
async def test_task_tool_rejects_child_recursion() -> None:
    """测试 child 调用 Task 会被拒绝。

    child run 不允许再次派发子代理，这防止了无限递归派发。
    """
    tool = TaskTool(FakeChildRunner())  # 不传入真实 runner，因为不会被执行到
    context = ExecutionContext(  # 构造 child 执行上下文（run_type="child"）
        run_id="child-run",  # child run ID
        session_id="session-1",  # 会话 ID
        metadata=None,  # 无额外元数据
        agent=create_fake_agent(),  # 假 Agent 实例
        run_type="child",  # child 类型，应触发递归禁止检查
        tool_call_id="call-1",  # 合法的 tool_call_id
        tool_name="Task",  # 当前工具名
    )

    result = await tool.call(  # 调用 Task 工具
        {"description": "x", "prompt": "x", "subagent_type": "Plan"},
        context,
    )

    assert result.is_error is True  # 应该返回错误
    assert ErrorCode.CHILD_AGENT_RECURSION_FORBIDDEN.value in result.content  # 确认递归禁止错误码


@pytest.mark.asyncio  # 标记为异步测试
async def test_task_tool_rejects_missing_tool_call_id() -> None:
    """测试 context.tool_call_id 为 None 时返回错误。

    只有携带有效 tool_call_id 的 master 上下文才能派发子代理。
    缺少 tool_call_id 时 TaskTool 应返回明确的错误信息。
    """
    tool = TaskTool(FakeChildRunner())  # 创建被测试的 TaskTool 实例
    context = ExecutionContext(  # 构造缺少 tool_call_id 的 master 执行上下文
        run_id="master-run",  # master run ID
        session_id="session-1",  # 会话 ID
        metadata=None,  # 无额外元数据
        agent=create_fake_agent(),  # 假 Agent 实例
        run_type="master",  # master 类型，满足非 child 检查
        tool_call_id=None,  # 缺少 tool_call_id，触发拒绝逻辑
        tool_name="Task",  # 当前工具名
    )

    result = await tool.call(  # 调用 Task 工具
        {"description": "制定计划", "prompt": "请制定实施计划", "subagent_type": "Plan"},  # 合法入参
        context,  # 但上下文缺少 tool_call_id
    )
    assert result.is_error is True  # 应该返回错误
    assert "缺少父级 tool_call_id" in result.content  # 确认错误消息包含预期文本


@pytest.mark.asyncio  # 标记为异步测试
async def test_task_tool_rejects_missing_required_params() -> None:
    """测试缺少 description、subagent_type 或 prompt 时返回错误。

    description、subagent_type 和 prompt 是 TaskTool 的必填参数，
    缺少任一参数时应在执行前返回明确的错误。
    """
    tool = TaskTool(FakeChildRunner())  # 创建被测试的 TaskTool 实例
    base_context = ExecutionContext(  # 构造合法的 master 执行上下文
        run_id="master-run",  # master run ID
        session_id="session-1",  # 会话 ID
        metadata=None,  # 无额外元数据
        agent=create_fake_agent(),  # 假 Agent 实例
        run_type="master",  # master 类型
        tool_call_id="call-1",  # 合法的 tool_call_id
        tool_name="Task",  # 当前工具名
    )

    # 场景 1：subagent_type 为空字符串
    result = await tool.call(  # 调用 Task 工具
        {"description": "制定计划", "prompt": "请制定实施计划", "subagent_type": ""},  # 空 subagent_type
        base_context,  # 合法上下文
    )
    assert result.is_error is True  # 应该返回错误
    assert "缺少 description、subagent_type 或 prompt" in result.content  # 确认错误消息

    # 场景 2：prompt 为空字符串
    result = await tool.call(  # 调用 Task 工具
        {"description": "制定计划", "prompt": "", "subagent_type": "Plan"},  # 空 prompt
        base_context,  # 合法上下文
    )
    assert result.is_error is True  # 应该返回错误
    assert "缺少 description、subagent_type 或 prompt" in result.content  # 确认错误消息

    # 场景 3：description 为空字符串
    result = await tool.call(  # 调用 Task 工具
        {"description": "", "prompt": "请制定实施计划", "subagent_type": "Plan"},  # 空 description
        base_context,  # 合法上下文
    )
    assert result.is_error is True  # 应该返回错误
    assert "缺少 description、subagent_type 或 prompt" in result.content  # 确认错误消息


@pytest.mark.asyncio  # 标记为异步测试
async def test_task_tool_passes_description_to_child_runner() -> None:
    """测试 TaskTool 会把 description 透传给 ChildAgentRunner。"""
    runner = FakeChildRunner()  # 创建 fake runner
    # TaskTool 传入包含 "Plan" 的 child_profiles，确保挂载校验通过
    mock_profile = MagicMock()  # 最小 mock profile
    tool = TaskTool(runner, child_profiles={"Plan": mock_profile})  # 创建被测试的 TaskTool 实例
    context = ExecutionContext(  # 构造 master 执行上下文
        run_id="master-run",  # master run ID
        session_id="session-1",  # 会话 ID
        metadata=None,  # 无额外元数据
        agent=create_fake_agent(),  # 假 Agent 实例
        run_type="master",  # master 类型，满足非 child 检查
        tool_call_id="call-1",  # 合法的 tool_call_id
        tool_name="Task",  # 当前工具名
    )

    await tool.call(  # 调用 Task 工具
        {
            "description": "继续分析目录结构",  # 任务描述
            "prompt": "请继续分析 app 目录",  # 任务 prompt
            "subagent_type": "Plan",  # 子代理类型
            "resume": "plan-existing",  # 已有的 child_id
        },
        context,  # 合法上下文
    )

    assert runner.last_call["description"] == "继续分析目录结构"  # 验证 description 正确透传


@pytest.mark.asyncio  # 标记为异步测试
async def test_task_tool_records_description_on_resume_failure() -> None:
    """测试 resume 校验失败时 description 仍被记录到 runner kwargs。"""
    runner = FakeChildRunner()  # 创建 fake runner
    runner.raise_on_resume = True  # 启用 resume 失败模拟
    # TaskTool 传入包含 "Plan" 的 child_profiles，确保挂载校验通过
    mock_profile = MagicMock()  # 最小 mock profile
    tool = TaskTool(runner, child_profiles={"Plan": mock_profile})  # 创建被测试的 TaskTool 实例
    context = ExecutionContext(  # 构造 master 执行上下文
        run_id="master-run",  # master run ID
        session_id="session-1",  # 会话 ID
        metadata=None,  # 无额外元数据
        agent=create_fake_agent(),  # 假 Agent 实例
        run_type="master",  # master 类型
        tool_call_id="call-1",  # 合法的 tool_call_id
        tool_name="Task",  # 当前工具名
    )

    result = await tool.call(  # 调用 Task 工具（预期返回错误）
        {
            "description": "失败的恢复描述",  # 任务描述
            "prompt": "请继续",  # 任务 prompt
            "subagent_type": "Plan",  # 子代理类型
            "resume": "plan-nonexistent",  # 不存在的 child_id
        },
        context,  # 合法上下文
    )

    assert result.is_error is True  # 工具应返回错误
    assert runner.last_call["description"] == "失败的恢复描述"  # description 仍应被记录


@pytest.mark.asyncio  # 标记为异步测试
async def test_task_tool_rejects_subagent_not_mounted_in_current_master() -> None:
    """验证 TaskTool 不允许调用未挂载到当前主代理的子代理。

    TaskTool 由 Container 在构建每个主代理的 profile 时创建，
    只传入该主代理挂载的子代理 profiles。
    如果 LLM 尝试调用未挂载的子代理类型，TaskTool 应在工具层返回错误，
    而不是透传到 ChildAgentRunner。
    """
    runner = FakeChildRunner()  # 创建 fake runner
    tool = TaskTool(runner, child_profiles={})  # 传入空 child_profiles，模拟无子代理挂载
    context = ExecutionContext(  # 构造 master 执行上下文
        run_id="master-run",  # master run ID
        session_id="session-1",  # 会话 ID
        metadata=None,  # 无额外元数据
        agent=create_fake_agent(),  # 假 Agent 实例
        run_type="master",  # master 类型，满足非 child 检查
        tool_call_id="call-1",  # 合法的 tool_call_id
        tool_name="Task",  # 当前工具名
    )

    result = await tool.call(  # 调用 Task 工具
        {
            "description": "规划任务",  # 任务描述
            "prompt": "拆解需求",  # 任务 prompt
            "subagent_type": "Planner",  # 子代理类型，不在 child_profiles 中
        },
        context,  # 合法上下文
    )

    assert result.is_error is True  # 应该返回错误
    assert ErrorCode.SUBAGENT_NOT_MOUNTED.value in result.content  # 确认错误码出现在结果中
    assert "Planner" in result.content  # 确认结果包含被拒绝的代理类型名
