"""任务工具集成测试。"""

from __future__ import annotations  # 启用未来注解

import json  # 导入 JSON 模块

import pytest  # 导入 pytest 测试框架

from app.core.models.execution_context import ExecutionContext  # 导入执行上下文
from app.core.models.tool import ToolResult  # 导入工具结果模型
from app.infra.store.redis_task_store import RedisTaskStore  # 导入任务存储
from app.infra.tools.task_create_tool import TaskCreateTool  # 导入任务创建工具
from app.infra.tools.task_get_tool import TaskGetTool  # 导入任务详情工具
from app.infra.tools.task_list_tool import TaskListTool  # 导入任务列表工具
from app.infra.tools.task_update_tool import TaskUpdateTool  # 导入任务更新工具
from app.services.task_service import TaskService  # 导入任务服务


@pytest.fixture  # 定义集成测试夹具
async def task_tools(fake_redis):
    """提供一组已注入真实 RedisTaskStore 的任务工具。"""
    store = RedisTaskStore(fake_redis, key_prefix="int")  # 使用独立前缀
    service = TaskService(store)  # 创建任务服务
    return {
        "create": TaskCreateTool(service),
        "get": TaskGetTool(service),
        "update": TaskUpdateTool(service),
        "list": TaskListTool(service),
    }


def _ctx(session_id: str) -> ExecutionContext:
    """构造执行上下文。"""
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
    )


async def assert_result_ok(result: ToolResult) -> dict:
    """辅助函数：断言结果无错误并返回解析后的 JSON。"""
    assert result.is_error is False, f"预期成功但失败: {result.content}"
    return json.loads(result.content)


class TestIdIncrementAndIsolation:
    """测试 ID 递增与会话隔离。"""

    async def test_ids_increment_in_session(self, task_tools):  # 测试同一会话递增
        t = task_tools
        r1 = await assert_result_ok(await t["create"].call({"subject": "T1", "description": "D"}, _ctx("s1")))
        r2 = await assert_result_ok(await t["create"].call({"subject": "T2", "description": "D"}, _ctx("s1")))
        r3 = await assert_result_ok(await t["create"].call({"subject": "T3", "description": "D"}, _ctx("s1")))
        assert r1["id"] == "1"
        assert r2["id"] == "2"
        assert r3["id"] == "3"

    async def test_session_isolation(self, task_tools):  # 测试跨会话隔离
        t = task_tools
        a = await assert_result_ok(await t["create"].call({"subject": "A", "description": "D"}, _ctx("sa")))
        b = await assert_result_ok(await t["create"].call({"subject": "B", "description": "D"}, _ctx("sb")))
        assert a["id"] == "1"
        assert b["id"] == "1"

        # 验证 session b 看不到 session a 的任务
        lst = await assert_result_ok(await t["list"].call({}, _ctx("sb")))
        assert len(lst) == 1
        assert lst[0]["subject"] == "B"


class TestDependencyChain:
    """测试依赖链建立与一致性。"""

    async def test_bidirectional_consistency(self, task_tools):  # 测试双向依赖一致
        t = task_tools
        ctx = _ctx("s2")
        await assert_result_ok(await t["create"].call({"subject": "T1", "description": "D"}, ctx))
        await assert_result_ok(await t["create"].call({"subject": "T2", "description": "D"}, ctx))
        await assert_result_ok(await t["create"].call({"subject": "T3", "description": "D"}, ctx))

        # 1 -> 2 -> 3
        await assert_result_ok(await t["update"].call({"taskId": "1", "addBlocks": ["2"]}, ctx))
        await assert_result_ok(await t["update"].call({"taskId": "2", "addBlocks": ["3"]}, ctx))

        t1 = await assert_result_ok(await t["get"].call({"taskId": "1"}, ctx))
        t2 = await assert_result_ok(await t["get"].call({"taskId": "2"}, ctx))
        t3 = await assert_result_ok(await t["get"].call({"taskId": "3"}, ctx))

        assert t1["blocks"] == ["2"]
        assert t1["blockedBy"] == []

        assert t2["blocks"] == ["3"]
        assert t2["blockedBy"] == ["1"]

        assert t3["blocks"] == []
        assert t3["blockedBy"] == ["2"]


class TestDeleteAndCleanup:
    """测试删除与反向引用清理。"""

    async def test_delete_cleans_refs(self, task_tools):  # 测试删除清理引用
        t = task_tools
        ctx = _ctx("s3")
        await assert_result_ok(await t["create"].call({"subject": "T1", "description": "D"}, ctx))
        await assert_result_ok(await t["create"].call({"subject": "T2", "description": "D"}, ctx))
        await assert_result_ok(await t["create"].call({"subject": "T3", "description": "D"}, ctx))

        await assert_result_ok(await t["update"].call({"taskId": "1", "addBlocks": ["2"]}, ctx))
        await assert_result_ok(await t["update"].call({"taskId": "2", "addBlocks": ["3"]}, ctx))

        del_result = await assert_result_ok(await t["update"].call({"taskId": "2", "status": "deleted"}, ctx))
        assert del_result["deleted"] is True

        # 任务 2 已不存在
        missing = await t["get"].call({"taskId": "2"}, ctx)
        assert missing.is_error is True

        t1 = await assert_result_ok(await t["get"].call({"taskId": "1"}, ctx))
        t3 = await assert_result_ok(await t["get"].call({"taskId": "3"}, ctx))
        assert t1["blocks"] == []
        assert t3["blockedBy"] == []

        # list 中也不应包含 2
        lst = await assert_result_ok(await t["list"].call({}, ctx))
        assert [item["id"] for item in lst] == ["1", "3"]


class TestErrorScenarios:
    """测试错误场景。"""

    async def test_missing_task_id_error(self, task_tools):  # 测试缺失任务 ID 报错
        t = task_tools
        ctx = _ctx("s4")
        result = await t["get"].call({"taskId": "99"}, ctx)
        assert result.is_error is True

    async def test_self_dependency_error(self, task_tools):  # 测试自依赖报错
        t = task_tools
        ctx = _ctx("s4")
        await assert_result_ok(await t["create"].call({"subject": "A", "description": "D"}, ctx))
        result = await t["update"].call({"taskId": "1", "addBlocks": ["1"]}, ctx)
        assert result.is_error is True
        assert "依赖自身" in result.content

    async def test_dependency_on_missing_task_error(self, task_tools):  # 测试依赖不存在任务报错
        t = task_tools
        ctx = _ctx("s4")
        await assert_result_ok(await t["create"].call({"subject": "A", "description": "D"}, ctx))
        result = await t["update"].call({"taskId": "1", "addBlockedBy": ["99"]}, ctx)
        assert result.is_error is True
        assert "依赖的任务不存在" in result.content
