"""MCP 客户端管理器单元测试。"""

from __future__ import annotations  # 启用未来注解，避免前向引用问题。

import asyncio  # 导入 asyncio，用于断言上下文进入与退出发生在同一任务。
from concurrent.futures import Future  # 导入 Future，用于驱动生命周期协程完成启动握手。
from datetime import timedelta  # 导入 timedelta，用于断言超时参数透传。
from pathlib import Path  # 导入 Path，用于构造临时配置文件路径。
from types import SimpleNamespace  # 导入轻量命名空间，便于伪造模块对象。

import pytest  # 导入 pytest 测试框架。

from app.config import Settings  # 导入 Settings，用于验证 from_settings 行为。
from app.infra.tools.mcp_client_manager import MCPClientManager, MCPConnection, MCPServerConfig  # 导入被测对象。


class FakeToolItem:  # 定义工具描述替身。
    """模拟 MCP SDK 返回的 Tool 描述对象。"""

    def __init__(self, name: str) -> None:  # 定义初始化方法。
        """保存工具名称。"""
        self.name = name  # 保存工具名称字段。
        self.description = f"description for {name}"  # 保存工具描述字段。
        self.inputSchema = {"type": "object"}  # 保存输入模式字段。


class FakeListToolsResult:  # 定义分页结果替身。
    """模拟 MCP SDK 的 ListToolsResult。"""

    def __init__(self, tools: list[FakeToolItem], next_cursor: str | None) -> None:  # 定义初始化方法。
        """保存当前页工具列表与下一页游标。"""
        self.tools = tools  # 保存当前页工具列表。
        self.nextCursor = next_cursor  # 保存下一页游标字段。


class FakePaginatedSession:  # 定义分页会话替身。
    """模拟支持 nextCursor 分页的 ClientSession。"""

    def __init__(self) -> None:  # 定义初始化方法。
        """初始化调用记录。"""
        self.calls: list[str | None] = []  # 保存所有 list_tools 调用时传入的 cursor。

    async def list_tools(self, cursor: str | None = None):  # 定义异步列工具方法。
        """根据 cursor 返回对应页数据。"""
        self.calls.append(cursor)  # 记录当前调用所使用的 cursor。
        if cursor is None:  # 判断是否为首页调用。
            return FakeListToolsResult([FakeToolItem("fetch")], "cursor-1")  # 返回第一页结果，并附带下一页游标。
        if cursor == "cursor-1":  # 判断是否为第二页调用。
            return FakeListToolsResult([FakeToolItem("search")], None)  # 返回最后一页结果，并清空下一页游标。
        raise AssertionError(f"unexpected cursor: {cursor}")  # 对未知游标直接失败，避免测试误过。


class TaskBoundTransportContext:  # 定义具备任务绑定约束的 transport 上下文替身。
    """模拟必须在同一任务里 enter/exit 的 transport 上下文。"""

    def __init__(self) -> None:  # 定义初始化方法。
        """初始化任务记录字段。"""
        self.enter_task: asyncio.Task[object] | None = None  # 记录进入 transport 时的任务对象。
        self.exit_task: asyncio.Task[object] | None = None  # 记录退出 transport 时的任务对象。

    async def __aenter__(self) -> tuple[object, object]:  # 定义异步进入方法。
        """记录进入任务，并返回占位读写流。"""
        self.enter_task = asyncio.current_task()  # 记录当前进入 transport 的任务对象。
        return object(), object()  # 返回最小占位读写流，满足连接创建签名。

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:  # 定义异步退出方法。
        """断言退出 transport 的任务必须与进入时保持一致。"""
        self.exit_task = asyncio.current_task()  # 记录当前退出 transport 的任务对象。
        if self.exit_task is not self.enter_task:  # 判断退出任务是否与进入任务不同。
            raise RuntimeError("transport exited in different task")  # 抛出明确错误，模拟底层任务绑定约束。


class FakeSessionForLifecycle:  # 定义生命周期测试会话替身。
    """模拟最小 MCP 会话，仅提供初始化与列工具能力。"""

    def __init__(self) -> None:  # 定义初始化方法。
        """初始化状态字段。"""
        self.initialized = False  # 记录 initialize 是否被调用。

    async def initialize(self) -> None:  # 定义异步初始化方法。
        """记录会话已完成初始化。"""
        self.initialized = True  # 标记 initialize 已被执行。

    async def list_tools(self, cursor: str | None = None) -> FakeListToolsResult:  # 定义异步列工具方法。
        """返回空工具结果，避免测试引入无关复杂度。"""
        return FakeListToolsResult([], None)  # 返回空工具列表，表示当前服务没有暴露工具。


class TaskBoundSessionContext:  # 定义具备任务绑定约束的 session 上下文替身。
    """模拟必须在同一任务里 enter/exit 的 session 上下文。"""

    def __init__(self, session: FakeSessionForLifecycle) -> None:  # 定义初始化方法。
        """保存真实会话替身。"""
        self._session = session  # 保存真实会话替身，供进入上下文时返回。
        self.enter_task: asyncio.Task[object] | None = None  # 记录进入 session 时的任务对象。
        self.exit_task: asyncio.Task[object] | None = None  # 记录退出 session 时的任务对象。

    async def __aenter__(self) -> FakeSessionForLifecycle:  # 定义异步进入方法。
        """记录进入任务，并返回真实会话替身。"""
        self.enter_task = asyncio.current_task()  # 记录当前进入 session 的任务对象。
        return self._session  # 返回真实会话替身，供管理器继续 initialize/list_tools。

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:  # 定义异步退出方法。
        """断言退出 session 的任务必须与进入时保持一致。"""
        self.exit_task = asyncio.current_task()  # 记录当前退出 session 的任务对象。
        if self.exit_task is not self.enter_task:  # 判断退出任务是否与进入任务不同。
            raise RuntimeError("session exited in different task")  # 抛出明确错误，模拟底层 TaskGroup 任务绑定限制。


class FakeClosableContext:  # 定义最小可关闭上下文替身。
    """用于生命周期测试的最小上下文。"""

    def __init__(self) -> None:  # 定义初始化方法。
        """初始化关闭状态。"""
        self.closed = False  # 记录上下文是否已关闭。

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:  # 定义异步退出方法。
        """记录退出动作。"""
        self.closed = True  # 标记上下文已关闭。


class FakeClientSessionContext:  # 定义 ClientSession 上下文替身。
    """模拟可进入的 ClientSession 上下文。"""

    last_instance: "FakeClientSessionContext | None" = None  # 保存最后一次构造的实例，便于断言参数透传。

    def __init__(self, read_stream, write_stream, read_timeout_seconds=None, **kwargs) -> None:  # 定义初始化方法。
        """保存初始化参数。"""
        _ = kwargs  # 当前测试不关心其他扩展参数。
        self.read_stream = read_stream  # 保存读流。
        self.write_stream = write_stream  # 保存写流。
        self.read_timeout_seconds = read_timeout_seconds  # 保存会话读超时。
        self.initialized = False  # 记录 initialize 是否被调用。
        self.closed = False  # 记录上下文是否已关闭。
        FakeClientSessionContext.last_instance = self  # 保存当前实例，供测试读取。

    async def __aenter__(self) -> "FakeClientSessionContext":  # 定义异步进入方法。
        """返回当前实例作为真实会话对象。"""
        return self  # 返回当前实例。

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:  # 定义异步退出方法。
        """记录关闭动作。"""
        self.closed = True  # 标记 session 上下文已关闭。

    async def initialize(self) -> None:  # 定义异步初始化方法。
        """记录初始化动作。"""
        self.initialized = True  # 标记 initialize 已执行。


class FakeStdioServerParameters:  # 定义 stdio 参数对象替身。
    """模拟 MCP SDK 的 StdioServerParameters。"""

    last_instance: "FakeStdioServerParameters | None" = None  # 保存最后一次构造实例，便于断言字段透传。

    def __init__(self, *, command: str, args: list[str], env: dict[str, str] | None, cwd: str | None) -> None:  # 定义初始化方法。
        """保存所有输入字段。"""
        self.command = command  # 保存启动命令。
        self.args = args  # 保存参数列表。
        self.env = env  # 保存环境变量。
        self.cwd = cwd  # 保存工作目录。
        FakeStdioServerParameters.last_instance = self  # 保存当前实例，供测试读取。


class FakeStdioTransportContext:  # 定义 stdio transport 上下文替身。
    """模拟 stdio transport 上下文。"""

    def __init__(self, server_parameters: FakeStdioServerParameters, enter_delay_seconds: float = 0.0) -> None:  # 定义初始化方法。
        """保存参数对象与进入延时。"""
        self.server_parameters = server_parameters  # 保存服务参数对象。
        self.enter_delay_seconds = enter_delay_seconds  # 保存进入时延时。
        self.closed = False  # 记录是否已关闭。

    async def __aenter__(self) -> tuple[object, object]:  # 定义异步进入方法。
        """返回占位读写流。"""
        if self.enter_delay_seconds > 0:  # 判断是否需要模拟慢启动。
            await asyncio.sleep(self.enter_delay_seconds)  # 通过 sleep 模拟阻塞启动。
        return object(), object()  # 返回最小占位读写流。

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:  # 定义异步退出方法。
        """记录关闭动作。"""
        self.closed = True  # 标记 transport 已关闭。


class FakeHTTPClient:  # 定义 httpx.AsyncClient 替身。
    """模拟可被上下文管理的 http client。"""

    def __init__(self) -> None:  # 定义初始化方法。
        """初始化进入与退出标记。"""
        self.entered = False  # 记录是否已进入。
        self.exited = False  # 记录是否已退出。

    async def __aenter__(self) -> "FakeHTTPClient":  # 定义异步进入方法。
        """返回自身。"""
        self.entered = True  # 标记已进入。
        return self  # 返回当前实例。

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:  # 定义异步退出方法。
        """记录退出动作。"""
        self.exited = True  # 标记已退出。


class FakeStreamableHTTPTransportContext:  # 定义 streamable-http transport 上下文替身。
    """模拟 streamable-http transport 上下文。"""

    def __init__(self) -> None:  # 定义初始化方法。
        """初始化关闭状态。"""
        self.closed = False  # 记录 transport 是否已关闭。

    async def __aenter__(self) -> tuple[object, object, object]:  # 定义异步进入方法。
        """返回占位读写流与额外返回值。"""
        return object(), object(), object()  # 返回 transport 所需的最小三元组。

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:  # 定义异步退出方法。
        """记录退出动作。"""
        self.closed = True  # 标记 transport 已退出。


class FakeHTTPXTimeout:  # 定义 httpx.Timeout 替身。
    """模拟 httpx.Timeout。"""

    def __init__(self, timeout: float, read: float) -> None:  # 定义初始化方法。
        """保存 timeout 参数。"""
        self.timeout = timeout  # 保存普通请求超时。
        self.read = read  # 保存读取超时。


class FakeHTTPXModule:  # 定义 httpx 模块替身。
    """模拟最小 httpx 模块。"""

    last_async_client_kwargs: dict | None = None  # 保存最后一次 AsyncClient 构造参数。

    Timeout = FakeHTTPXTimeout  # 把 Timeout 类型映射到测试替身。

    @staticmethod  # 声明静态方法。
    def AsyncClient(**kwargs) -> SimpleNamespace:  # 定义 AsyncClient 构造替身。
        """记录构造参数并返回占位 client。"""
        FakeHTTPXModule.last_async_client_kwargs = kwargs  # 保存构造参数，供测试断言。
        return SimpleNamespace(kwargs=kwargs)  # 返回最小占位对象。


def _write_config(tmp_path: Path, text: str) -> Path:  # 定义写配置辅助函数。
    """把给定文本写入临时配置文件。"""
    config_path = tmp_path / "mcp_servers.json"  # 构造临时配置文件路径。
    config_path.write_text(text, encoding="utf-8")  # 写入测试配置文本。
    return config_path  # 返回配置文件路径。


@pytest.mark.asyncio  # 标记为异步测试。
async def test_list_all_tool_items_follows_next_cursor_until_all_pages_loaded() -> None:  # 定义分页拉全测试。
    """验证管理器会沿着 nextCursor 一直翻页，直到拉取全量工具。"""
    session = FakePaginatedSession()  # 创建分页会话替身。
    manager = MCPClientManager(config_path="unused.json", configs=[])  # 创建最小管理器实例。

    tool_items = await manager._list_all_tool_items(session)  # 调用待测辅助方法，拉取全量工具描述。

    assert session.calls == [None, "cursor-1"]  # 断言分页调用顺序符合预期。
    assert [tool.name for tool in tool_items] == ["fetch", "search"]  # 断言最终拿到了所有页的工具。


@pytest.mark.asyncio  # 标记为异步测试。
async def test_aclose_keeps_mcp_context_exit_in_same_background_task(monkeypatch) -> None:  # 定义生命周期任务绑定测试。
    """验证关闭阶段会在同一个后台任务里退出 MCP 上下文。"""
    manager = MCPClientManager(  # 创建最小管理器实例。
        config_path="unused.json",  # 传入占位配置路径。
        configs=[MCPServerConfig(server_id="demo", transport="stdio", command="demo")],  # 构造一个最小服务配置，触发真实生命周期线程。
    )
    transport_context = TaskBoundTransportContext()  # 创建 transport 上下文替身。
    session = FakeSessionForLifecycle()  # 创建真实会话替身。
    session_context = TaskBoundSessionContext(session)  # 创建 session 上下文替身。

    async def fake_open_connection(config: MCPServerConfig) -> MCPConnection:  # 定义连接打开替身。
        """在当前生命周期任务里手动 enter 两层上下文，模拟真实连接建立流程。"""
        read_stream, write_stream = await transport_context.__aenter__()  # 在生命周期任务里进入 transport 上下文。
        opened_session = await session_context.__aenter__()  # 在同一个生命周期任务里进入 session 上下文。
        await opened_session.initialize()  # 模拟真实启动阶段会先执行 initialize。
        return MCPConnection(  # 返回最小连接载体。
            server_id=config.server_id,  # 记录服务标识。
            transport_context=transport_context,  # 保存 transport 上下文替身，供关闭阶段退出。
            session_context=session_context,  # 保存 session 上下文替身，供关闭阶段退出。
            session=opened_session,  # 保存真实会话替身。
        )

    monkeypatch.setattr(manager, "_open_connection", fake_open_connection)  # 拦截真实连接建立逻辑，改为使用任务绑定替身。

    manager.start()  # 启动管理器，在线程后台建立连接。

    await manager.aclose()  # 关闭管理器，验证退出仍发生在同一个后台生命周期任务中。

    assert session.initialized is True  # 断言生命周期任务确实完成了会话初始化。
    assert session_context.enter_task is not None  # 断言 session 上下文已被进入。
    assert session_context.exit_task is session_context.enter_task  # 断言 session 上下文在同一个任务里退出。
    assert transport_context.enter_task is not None  # 断言 transport 上下文已被进入。
    assert transport_context.exit_task is transport_context.enter_task  # 断言 transport 上下文在同一个任务里退出。


def test_load_configs_supports_json_comments_and_new_fields(tmp_path: Path) -> None:  # 定义配置解析测试。
    """验证配置文件支持注释与新增字段。"""
    config_path = _write_config(  # 写入带注释的临时配置文件。
        tmp_path,  # 传入临时目录。
        """
        [
          // streamable-http 示例
          {
            "server_id": "remote-demo",
            "name": "Remote Demo",
            "description": "demo remote server",
            "enabled": true,
            "transport": "streamable-http",
            "url": "https://example.com/mcp",
            "timeout_seconds": 15,
            "headers": {
              "Authorization": "Bearer demo"
            },
            "verify_ssl": false,
            "follow_redirects": false
          },
          /* stdio 示例 */
          {
            "server_id": "stdio-demo",
            "transport": "stdio",
            "enabled": false,
            "command": "python",
            "args": ["server.py"],
            "env": {
              "ENV": "test"
            },
            "cwd": "/tmp/demo",
            "startup_timeout_seconds": 8
          }
        ]
        """,
    )

    configs = MCPClientManager._load_configs(config_path)  # 解析测试配置。

    assert len(configs) == 2  # 断言两个配置项都被成功解析。
    assert configs[0].name == "Remote Demo"  # 断言显示名字段已被保留。
    assert configs[0].description == "demo remote server"  # 断言描述字段已被保留。
    assert configs[0].headers == {"Authorization": "Bearer demo"}  # 断言 headers 字段已解析。
    assert configs[0].verify_ssl is False  # 断言 verify_ssl 字段已解析。
    assert configs[0].follow_redirects is False  # 断言 follow_redirects 字段已解析。
    assert configs[0].timeout_seconds == 15.0  # 断言 timeout_seconds 已统一转成 float。
    assert configs[1].enabled is False  # 断言禁用开关已解析。
    assert configs[1].cwd == "/tmp/demo"  # 断言 cwd 字段已解析。
    assert configs[1].startup_timeout_seconds == 8.0  # 断言启动超时字段已解析。


def test_load_configs_rejects_unknown_fields(tmp_path: Path) -> None:  # 定义未知字段校验测试。
    """验证未知字段不会再被静默忽略。"""
    config_path = _write_config(  # 写入带未知字段的配置。
        tmp_path,  # 传入临时目录。
        """
        [
          {
            "server_id": "demo",
            "transport": "streamable-http",
            "url": "https://example.com/mcp",
            "metadata": {
              "foo": "bar"
            }
          }
        ]
        """,
    )

    with pytest.raises(ValueError, match="metadata"):  # 断言未知字段会触发启动期错误。
        MCPClientManager._load_configs(config_path)  # 解析配置，触发校验。


def test_from_settings_does_not_start_when_all_configs_disabled(tmp_path: Path, monkeypatch) -> None:  # 定义禁用配置测试。
    """验证当所有服务都被禁用时，管理器不会启动后台线程。"""
    config_path = _write_config(  # 写入全禁用配置。
        tmp_path,  # 传入临时目录。
        """
        [
          {
            "server_id": "demo",
            "transport": "streamable-http",
            "enabled": false,
            "url": "https://example.com/mcp"
          }
        ]
        """,
    )
    started = False  # 记录 start 是否被调用。

    def fake_start(self) -> None:  # 定义 start 替身。
        """记录启动动作。"""
        nonlocal started  # 允许修改外层变量。
        started = True  # 标记管理器尝试启动。

    monkeypatch.setattr(MCPClientManager, "start", fake_start)  # 拦截 start 调用。

    manager = MCPClientManager.from_settings(  # 基于配置创建管理器。
        Settings(redis_url="redis://localhost:6379", mcp_servers_config_path=str(config_path))  # 传入最小合法 Settings。
    )

    assert started is False  # 断言未触发后台启动。
    assert manager.list_tools() == []  # 断言初始工具列表为空。


@pytest.mark.asyncio  # 标记为异步测试。
async def test_run_lifecycle_skips_disabled_configs(monkeypatch) -> None:  # 定义禁用配置跳过测试。
    """验证生命周期任务会跳过 enabled=false 的服务。"""
    configs = [  # 构造一个禁用服务和一个启用服务。
        MCPServerConfig(server_id="disabled", transport="stdio", enabled=False, command="demo"),  # 禁用服务。
        MCPServerConfig(server_id="enabled", transport="stdio", enabled=True, command="demo"),  # 启用服务。
    ]
    manager = MCPClientManager(config_path="unused.json", configs=configs)  # 创建管理器实例。
    opened_server_ids: list[str] = []  # 记录真正进入连接打开阶段的服务标识。

    async def fake_open_connection(config: MCPServerConfig) -> MCPConnection:  # 定义连接打开替身。
        """只记录启用配置的 server_id。"""
        opened_server_ids.append(config.server_id)  # 记录被打开的服务标识。
        manager._close_event.set()  # 让生命周期任务在本次循环后尽快退出。
        return MCPConnection(  # 返回最小连接载体。
            server_id=config.server_id,  # 保存服务标识。
            transport_context=FakeClosableContext(),  # 提供可关闭 transport 上下文。
            session_context=FakeClosableContext(),  # 提供可关闭 session 上下文。
            session=FakeSessionForLifecycle(),  # 提供最小 session 对象。
        )

    async def fake_list_all_tool_items(session) -> list[FakeToolItem]:  # 定义工具发现替身。
        """返回空工具集，避免测试引入无关复杂度。"""
        _ = session  # 当前测试不关心 session 内容。
        return []  # 返回空工具列表。

    monkeypatch.setattr(manager, "_open_connection", fake_open_connection)  # 拦截真实连接建立。
    monkeypatch.setattr(manager, "_list_all_tool_items", fake_list_all_tool_items)  # 拦截真实工具发现。

    started_future: Future[None] = Future()  # 创建启动同步句柄。
    await manager._run_lifecycle(started_future)  # 直接执行生命周期协程。

    assert opened_server_ids == ["enabled"]  # 断言仅启用服务进入了打开阶段。


@pytest.mark.asyncio  # 标记为异步测试。
async def test_open_stdio_connection_passes_cwd_and_timeout_to_sdk(monkeypatch) -> None:  # 定义 stdio 参数透传测试。
    """验证 stdio 连接会透传 cwd 与会话超时。"""
    manager = MCPClientManager(config_path="unused.json", configs=[])  # 创建最小管理器实例。

    def fake_stdio_client(server_parameters: FakeStdioServerParameters) -> FakeStdioTransportContext:  # 定义 stdio_client 替身。
        """返回最小 transport 上下文。"""
        return FakeStdioTransportContext(server_parameters=server_parameters)  # 返回 transport 替身。

    def fake_import_module(module_name: str):  # 定义模块导入替身。
        """按模块名返回测试替身模块。"""
        if module_name == "mcp":  # 处理主模块导入。
            return SimpleNamespace(ClientSession=FakeClientSessionContext, StdioServerParameters=FakeStdioServerParameters)  # 返回最小 mcp 模块替身。
        if module_name == "mcp.client.stdio":  # 处理 stdio 模块导入。
            return SimpleNamespace(stdio_client=fake_stdio_client)  # 返回 stdio 模块替身。
        raise AssertionError(f"unexpected module import: {module_name}")  # 对未知导入直接失败。

    monkeypatch.setattr(manager, "_import_module", fake_import_module)  # 拦截模块导入。

    connection = await manager._open_connection(  # 打开 stdio 连接。
        MCPServerConfig(  # 构造测试配置。
            server_id="stdio-demo",  # 设置服务标识。
            transport="stdio",  # 指定 stdio 模式。
            command="python",  # 设置启动命令。
            args=["server.py"],  # 设置参数列表。
            env={"ENV": "test"},  # 设置环境变量。
            cwd="/tmp/demo",  # 设置工作目录。
            timeout_seconds=45,  # 设置会话超时。
        )
    )

    assert FakeStdioServerParameters.last_instance is not None  # 断言参数对象确实被创建。
    assert FakeStdioServerParameters.last_instance.cwd == "/tmp/demo"  # 断言 cwd 已透传到 SDK 参数对象。
    assert FakeStdioServerParameters.last_instance.args == ["server.py"]  # 断言 args 已透传到 SDK 参数对象。
    assert FakeClientSessionContext.last_instance is not None  # 断言 session 上下文已被创建。
    assert FakeClientSessionContext.last_instance.read_timeout_seconds == timedelta(seconds=45)  # 断言通用超时已映射到会话读超时。
    assert connection.session.initialized is True  # 断言会话初始化已执行。


@pytest.mark.asyncio  # 标记为异步测试。
async def test_open_stdio_connection_honors_startup_timeout(monkeypatch) -> None:  # 定义 stdio 启动超时测试。
    """验证 stdio 启动阶段会受到 startup_timeout_seconds 约束。"""
    manager = MCPClientManager(config_path="unused.json", configs=[])  # 创建最小管理器实例。

    def fake_stdio_client(server_parameters: FakeStdioServerParameters) -> FakeStdioTransportContext:  # 定义慢启动 stdio_client 替身。
        """返回带进入延时的 transport 上下文。"""
        return FakeStdioTransportContext(server_parameters=server_parameters, enter_delay_seconds=0.05)  # 模拟慢启动。

    def fake_import_module(module_name: str):  # 定义模块导入替身。
        """按模块名返回测试替身模块。"""
        if module_name == "mcp":  # 处理主模块导入。
            return SimpleNamespace(ClientSession=FakeClientSessionContext, StdioServerParameters=FakeStdioServerParameters)  # 返回最小 mcp 模块替身。
        if module_name == "mcp.client.stdio":  # 处理 stdio 模块导入。
            return SimpleNamespace(stdio_client=fake_stdio_client)  # 返回 stdio 模块替身。
        raise AssertionError(f"unexpected module import: {module_name}")  # 对未知导入直接失败。

    monkeypatch.setattr(manager, "_import_module", fake_import_module)  # 拦截模块导入。

    with pytest.raises(TimeoutError, match="stdio-demo"):  # 断言超时错误会带上服务标识。
        await manager._open_connection(  # 打开 stdio 连接，触发超时。
            MCPServerConfig(  # 构造测试配置。
                server_id="stdio-demo",  # 设置服务标识。
                transport="stdio",  # 指定 stdio 模式。
                command="python",  # 设置启动命令。
                startup_timeout_seconds=0.01,  # 设置极短启动超时。
            )
        )


def test_create_streamable_http_client_uses_configured_http_settings(monkeypatch) -> None:  # 定义 HTTP client 构造测试。
    """验证 streamable-http 会基于配置创建定制化 http client。"""
    manager = MCPClientManager(config_path="unused.json", configs=[])  # 创建最小管理器实例。

    def fake_import_module(module_name: str):  # 定义模块导入替身。
        """按模块名返回测试替身模块。"""
        if module_name == "httpx":  # 处理 httpx 模块导入。
            return FakeHTTPXModule  # 返回 httpx 模块替身。
        if module_name == "mcp.shared._httpx_utils":  # 处理 MCP 默认 HTTP 配置模块导入。
            return SimpleNamespace(MCP_DEFAULT_TIMEOUT=30.0, MCP_DEFAULT_SSE_READ_TIMEOUT=300.0)  # 返回默认常量替身。
        raise AssertionError(f"unexpected module import: {module_name}")  # 对未知导入直接失败。

    monkeypatch.setattr(manager, "_import_module", fake_import_module)  # 拦截模块导入。

    client = manager._create_streamable_http_client(  # 构造 HTTP client。
        MCPServerConfig(  # 传入测试配置。
            server_id="remote-demo",  # 设置服务标识。
            transport="streamable-http",  # 指定 HTTP 模式。
            url="https://example.com/mcp",  # 设置服务地址。
            headers={"Authorization": "Bearer demo"},  # 设置自定义请求头。
            verify_ssl=False,  # 设置关闭证书校验。
            follow_redirects=False,  # 设置关闭重定向。
            timeout_seconds=12,  # 设置普通请求超时。
        )
    )

    assert client.kwargs["headers"] == {"Authorization": "Bearer demo"}  # 断言 headers 已注入 AsyncClient。
    assert client.kwargs["verify"] is False  # 断言 verify_ssl 已注入 AsyncClient。
    assert client.kwargs["follow_redirects"] is False  # 断言 follow_redirects 已注入 AsyncClient。
    assert client.kwargs["timeout"].timeout == 12  # 断言普通请求超时已按配置覆盖。
    assert client.kwargs["timeout"].read == 300.0  # 断言 SSE 读取超时仍保留 MCP SDK 默认值。


@pytest.mark.asyncio  # 标记为异步测试。
async def test_open_streamable_http_connection_uses_managed_http_client(monkeypatch) -> None:  # 定义 HTTP 连接打开测试。
    """验证 streamable-http 连接会托管自建 http client 生命周期。"""
    manager = MCPClientManager(config_path="unused.json", configs=[])  # 创建最小管理器实例。
    http_client = FakeHTTPClient()  # 创建 http client 替身。
    transport_context = FakeStreamableHTTPTransportContext()  # 创建 transport 上下文替身。
    called: dict[str, object] = {}  # 记录 transport 构造调用参数。

    def fake_streamable_http_client(url: str, *, http_client: FakeHTTPClient):  # 定义 transport 构造替身。
        """记录入参并返回 transport 上下文。"""
        called["url"] = url  # 记录传入的 URL。
        called["http_client"] = http_client  # 记录传入的 http client。
        return transport_context  # 返回 transport 上下文替身。

    def fake_import_module(module_name: str):  # 定义模块导入替身。
        """按模块名返回测试替身模块。"""
        if module_name == "mcp":  # 处理主模块导入。
            return SimpleNamespace(ClientSession=FakeClientSessionContext)  # 返回最小 mcp 模块替身。
        if module_name == "mcp.client.streamable_http":  # 处理 streamable-http 模块导入。
            return SimpleNamespace(streamable_http_client=fake_streamable_http_client)  # 返回 transport 模块替身。
        raise AssertionError(f"unexpected module import: {module_name}")  # 对未知导入直接失败。

    monkeypatch.setattr(manager, "_import_module", fake_import_module)  # 拦截模块导入。
    monkeypatch.setattr(manager, "_create_streamable_http_client", lambda config: http_client)  # 拦截 http client 创建，返回测试替身。

    connection = await manager._open_connection(  # 打开 HTTP 连接。
        MCPServerConfig(  # 构造测试配置。
            server_id="remote-demo",  # 设置服务标识。
            transport="streamable-http",  # 指定 HTTP 模式。
            url="https://example.com/mcp",  # 设置服务地址。
            timeout_seconds=9,  # 设置会话超时。
        )
    )
    manager._connections.append(connection)  # 手动登记连接，便于复用真实关闭逻辑。

    assert called == {"url": "https://example.com/mcp", "http_client": http_client}  # 断言 transport 构造接收到了预期参数。
    assert http_client.entered is True  # 断言连接建立阶段已进入 http client 上下文。
    assert http_client.exited is False  # 断言连接尚未关闭前，http client 仍保持打开。
    assert FakeClientSessionContext.last_instance is not None  # 断言 session 上下文已被创建。
    assert FakeClientSessionContext.last_instance.read_timeout_seconds == timedelta(seconds=9)  # 断言会话级超时已透传。

    await manager._async_close()  # 执行关闭流程，验证资源回收。

    assert transport_context.closed is True  # 断言 transport 已被关闭。
    assert http_client.exited is True  # 断言自建 http client 也被统一关闭。
