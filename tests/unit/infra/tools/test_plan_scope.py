"""测试 ExecutionContext.resolve_plan_session_id 隔离逻辑。"""

from __future__ import annotations

from app.core.models.execution_context import ExecutionContext


def _make_ctx(*, session_id: str = "s1", run_type: str = "master",
              child_id: str | None = None) -> ExecutionContext:
    """快速构造执行上下文辅助函数。"""
    from app.core.models.agent import Agent
    agent = Agent(
        agent_id="a1",
        name="Test",
        model="gpt-4",
        system_prompt="test",
        temperature=0.0,
    )
    return ExecutionContext(
        run_id="r1",
        session_id=session_id,
        metadata={},
        agent=agent,
        run_type=run_type,
        child_id=child_id,
    )


class TestResolvePlanSessionId:
    """测试 resolve_plan_session_id 在不同上下文下的行为。"""

    def test_master_returns_plain_session_id(self):
        """主代理（run_type="master"）应返回原始 session_id。"""
        ctx = _make_ctx()
        assert ctx.resolve_plan_session_id() == "s1"

    def test_child_without_child_id_returns_plain(self):
        """子代理但 child_id 为 None 时应返回原始 session_id（防御性）。"""
        ctx = _make_ctx(run_type="child", child_id=None)
        assert ctx.resolve_plan_session_id() == "s1"

    def test_child_with_child_id_returns_composite(self):
        """子代理且 child_id 非空时应返回组合键。"""
        ctx = _make_ctx(run_type="child", child_id="plan-abc")
        assert ctx.resolve_plan_session_id() == "s1:child:plan-abc"

    def test_master_with_child_id_returns_plain(self):
        """主代理即便携带 child_id 也不应使用隔离键（防御性）。"""
        ctx = _make_ctx(run_type="master", child_id="plan-abc")
        assert ctx.resolve_plan_session_id() == "s1"

    def test_different_child_ids_produce_different_keys(self):
        """不同 child_id 应产生不同的组合键，保证隔离。"""
        ctx_a = _make_ctx(run_type="child", child_id="plan-a")
        ctx_b = _make_ctx(run_type="child", child_id="plan-b")
        assert ctx_a.resolve_plan_session_id() == "s1:child:plan-a"
        assert ctx_b.resolve_plan_session_id() == "s1:child:plan-b"
        assert ctx_a.resolve_plan_session_id() != ctx_b.resolve_plan_session_id()
