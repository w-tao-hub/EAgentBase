"""任务工具单元测试。"""

from __future__ import annotations

import json

import pytest

from app.core.models.execution_context import ExecutionContext
from app.core.models.tool import ToolRegistry, ToolResult
from app.infra.store.redis_task_store import RedisTaskStore
from app.infra.tools.plan_create_tool import PlanCreateTool
from app.infra.tools.plan_get_tool import PlanGetTool
from app.infra.tools.plan_list_tool import PlanListTool
from app.infra.tools.plan_update_tool import PlanUpdateTool
from app.services.task_service import TaskService


@pytest.fixture  # 定义测试夹具
async def tools(fake_redis):
    """为每个测试提供一组已注入 task_service 的计划工具实例。"""
    store = RedisTaskStore(fake_redis, key_prefix="test")  # 创建独立前缀存储
    service = TaskService(store)  # 创建任务服务
    return {
        "create": PlanCreateTool(service),
        "get": PlanGetTool(service),
        "update": PlanUpdateTool(service),
        "list": PlanListTool(service),
    }


def _ctx(session_id: str = "s1", run_type: str = "master", child_id: str | None = None) -> ExecutionContext:
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


class TestToolProperties:
    """测试工具基本属性。"""

    def test_names(self, tools):  # 测试工具名称
        assert tools["create"].name == "plan_create"
        assert tools["get"].name == "plan_get"
        assert tools["update"].name == "plan_update"
        assert tools["list"].name == "plan_list"

    def test_input_schemas(self, tools):  # 测试输入 schema 结构
        """各工具应具备合理的 JSON Schema 结构。"""
        assert tools["create"].input_schema["type"] == "object"
        assert "subject" in tools["create"].input_schema["properties"]
        assert "taskId" in tools["get"].input_schema["properties"]
        assert "status" in tools["update"].input_schema["properties"]
        assert tools["list"].input_schema["type"] == "object"


class TestPlanCreateTool:
    """测试 PlanCreateTool。"""

    async def test_create_success(self, tools):  # 测试成功创建
        result = await tools["create"].call(
            {"subject": "任务", "description": "描述", "activeForm": "正在做"},
            _ctx(),
        )
        assert isinstance(result, ToolResult)  # 验证返回类型
        assert result.is_error is False  # 无错误
        data = json.loads(result.content)
        assert data["id"] == "1"
        assert data["activeForm"] == "正在做"
        assert data["status"] == "pending"

    async def test_create_without_active_form(self, tools):  # 测试缺失 activeForm 仍可创建
        result = await tools["create"].call(
            {"subject": "任务", "description": "描述"},
            _ctx(),
        )
        assert result.is_error is False
        assert json.loads(result.content)["activeForm"] is None

    async def test_create_missing_required(self, tools):  # 测试缺失必填字段报错
        result = await tools["create"].call({"subject": "任务"}, _ctx())
        assert result.is_error is True
        assert "description" in result.content

    async def test_create_child_isolation(self, tools):  # 测试子代理创建 task 的命名空间隔离
        master_ctx = _ctx("s1")
        child_ctx = _ctx("s1", run_type="child", child_id="plan-abc")

        master_result = await tools["create"].call(
            {"subject": "Master任务", "description": "描述"}, master_ctx
        )
        assert master_result.is_error is False

        child_result = await tools["create"].call(
            {"subject": "Child任务", "description": "描述"}, child_ctx
        )
        assert child_result.is_error is False

        master_data = json.loads(master_result.content)
        child_data = json.loads(child_result.content)

        # 两个命名空间的任务 ID 各自从 1 开始
        assert master_data["id"] == "1"
        assert child_data["id"] == "1"
        assert master_data["subject"] == "Master任务"
        assert child_data["subject"] == "Child任务"


class TestPlanGetTool:
    """测试 PlanGetTool。"""

    async def test_get_success(self, tools):  # 测试成功获取
        await tools["create"].call({"subject": "任务", "description": "描述"}, _ctx())
        result = await tools["get"].call({"taskId": "1"}, _ctx())
        assert result.is_error is False
        data = json.loads(result.content)
        assert data["subject"] == "任务"
        assert "description" in data  # 完整字段

    async def test_get_missing_task_id(self, tools):  # 测试缺失 taskId 报错
        result = await tools["get"].call({}, _ctx())
        assert result.is_error is True
        assert "taskId" in result.content

    async def test_get_missing_task(self, tools):  # 测试获取不存在的任务报错
        result = await tools["get"].call({"taskId": "99"}, _ctx())
        assert result.is_error is True
        assert "不存在" in result.content


class TestPlanUpdateTool:
    """测试 PlanUpdateTool。"""

    async def test_update_success(self, tools):  # 测试成功更新
        await tools["create"].call({"subject": "原", "description": "描述"}, _ctx())
        result = await tools["update"].call(
            {"taskId": "1", "status": "in_progress"},
            _ctx(),
        )
        assert result.is_error is False
        assert json.loads(result.content)["status"] == "in_progress"

    async def test_delete_returns_deleted_json(self, tools):  # 测试删除返回确认 JSON
        await tools["create"].call({"subject": "任务", "description": "描述"}, _ctx())
        result = await tools["update"].call(
            {"taskId": "1", "status": "deleted"},
            _ctx(),
        )
        assert result.is_error is False
        data = json.loads(result.content)
        assert data["taskId"] == "1"
        assert data["deleted"] is True

    async def test_update_missing_task(self, tools):  # 测试更新不存在的任务报错
        result = await tools["update"].call(
            {"taskId": "99", "status": "completed"},
            _ctx(),
        )
        assert result.is_error is True
        assert "不存在" in result.content

    async def test_self_dependency_error(self, tools):  # 测试自依赖报错
        await tools["create"].call({"subject": "A", "description": "描述"}, _ctx())
        result = await tools["update"].call(
            {"taskId": "1", "addBlocks": ["1"]},
            _ctx(),
        )
        assert result.is_error is True
        assert "依赖自身" in result.content

    async def test_missing_dependency_error(self, tools):  # 测试依赖不存在的任务报错
        await tools["create"].call({"subject": "A", "description": "描述"}, _ctx())
        result = await tools["update"].call(
            {"taskId": "1", "addBlockedBy": ["99"]},
            _ctx(),
        )
        assert result.is_error is True
        assert "依赖的任务不存在" in result.content


class TestPlanListTool:
    """测试 PlanListTool。"""

    async def test_list_empty(self, tools):  # 测试空列表
        result = await tools["list"].call({}, _ctx())
        assert result.is_error is False
        assert json.loads(result.content) == []

    async def test_list_returns_summaries(self, tools):  # 测试返回摘要数组
        await tools["create"].call({"subject": "A", "description": "描述A"}, _ctx())
        await tools["create"].call({"subject": "B", "description": "描述B"}, _ctx())
        result = await tools["list"].call({}, _ctx())
        assert result.is_error is False
        tasks = json.loads(result.content)
        assert len(tasks) == 2
        assert set(tasks[0].keys()) == {"id", "subject", "status", "owner", "blockedBy"}
        # 验证按数字 ID 升序
        assert tasks[0]["id"] == "1"
        assert tasks[1]["id"] == "2"

    async def test_list_excludes_deleted(self, tools):  # 测试列表不包含已删除任务
        await tools["create"].call({"subject": "A", "description": "描述"}, _ctx())
        await tools["create"].call({"subject": "B", "description": "描述"}, _ctx())
        await tools["update"].call({"taskId": "1", "status": "deleted"}, _ctx())
        result = await tools["list"].call({}, _ctx())
        tasks = json.loads(result.content)
        assert len(tasks) == 1
        assert tasks[0]["id"] == "2"

    async def test_list_child_isolation(self, tools):  # 测试子代理的 task 列表隔离
        master_ctx = _ctx("s1")
        child_ctx = _ctx("s1", run_type="child", child_id="plan-abc")

        await tools["create"].call({"subject": "MasterTask", "description": "D"}, master_ctx)
        await tools["create"].call({"subject": "ChildTask", "description": "D"}, child_ctx)

        master_list = json.loads((await tools["list"].call({}, master_ctx)).content)
        child_list = json.loads((await tools["list"].call({}, child_ctx)).content)

        assert len(master_list) == 1
        assert master_list[0]["subject"] == "MasterTask"
        assert len(child_list) == 1
        assert child_list[0]["subject"] == "ChildTask"


class TestPlanToolRegistrySmoke:
    """测试计划工具注册后的基础冒烟链路。"""

    async def test_registered_plan_tools_can_get_and_call(self, tools):  # 测试注册后可按名取回并调用
        registry = ToolRegistry()  # 创建工具注册表，模拟容器中的统一注册行为
        registry.register(tools["create"])  # 注册计划创建工具
        registry.register(tools["get"])  # 注册计划详情工具
        registry.register(tools["update"])  # 注册计划更新工具
        registry.register(tools["list"])  # 注册计划列表工具

        assert registry.list_tools() == [  # 验证四个新工具都已进入注册表
            "plan_create",
            "plan_get",
            "plan_update",
            "plan_list",
        ]

        context = _ctx("registry-smoke")  # 为整条冒烟链路固定一个独立会话

        create_tool = registry.get("plan_create")  # 按容器注册名取回创建工具
        assert create_tool is tools["create"]  # 确认注册表返回的就是原始实例
        create_result = await create_tool.call(  # 先创建一条任务，供后续 get/update/list 复用
            {"subject": "冒烟任务", "description": "验证 ToolRegistry 注册链路"},
            context,
        )
        assert create_result.is_error is False
        assert json.loads(create_result.content)["id"] == "1"

        get_tool = registry.get("plan_get")  # 按名取回详情工具
        assert get_tool is tools["get"]
        get_result = await get_tool.call({"taskId": "1"}, context)  # 验证注册后的详情查询可正常工作
        assert get_result.is_error is False
        assert json.loads(get_result.content)["subject"] == "冒烟任务"

        update_tool = registry.get("plan_update")  # 按名取回更新工具
        assert update_tool is tools["update"]
        update_result = await update_tool.call(  # 验证注册后的更新链路可正常工作
            {"taskId": "1", "status": "in_progress"},
            context,
        )
        assert update_result.is_error is False
        assert json.loads(update_result.content)["status"] == "in_progress"

        list_tool = registry.get("plan_list")  # 按名取回列表工具
        assert list_tool is tools["list"]
        list_result = await list_tool.call({}, context)  # 验证注册后的列表链路可正常工作
        assert list_result.is_error is False
        tasks = json.loads(list_result.content)
        assert len(tasks) == 1
        assert tasks[0]["id"] == "1"
