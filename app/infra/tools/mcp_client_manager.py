"""MCP 客户端管理器。"""

from __future__ import annotations  # 启用未来注解，避免前向引用问题。

import asyncio  # 导入 asyncio，用于后台事件循环、超时控制与跨线程调度。
import contextlib  # 导入上下文工具，用于组合关闭 http client 与 transport。
import importlib  # 导入 importlib，用于按需加载官方 MCP SDK 与 httpx。
import json  # 导入 JSON 模块，用于解析配置文件。
from concurrent.futures import Future  # 导入 Future 类型，用于跨线程等待协程结果。
from dataclasses import dataclass  # 导入数据类装饰器，用于定义配置与连接载体。
from datetime import timedelta  # 导入 timedelta，用于构造 session 级读超时。
from pathlib import Path  # 导入 Path，用于处理配置文件路径。
from threading import Event, Thread  # 导入线程与事件，用于维护后台事件循环。
from typing import Any  # 导入通用类型提示。

from app.config import Settings  # 导入配置模型，用于读取配置文件路径。
from app.core.models.tool import Tool  # 导入工具抽象，用于返回已发现的工具列表。
from app.infra.tools.mcp_adapter import MCPToolAdapter  # 导入 MCP 工具适配器，用于构造本地工具实例。

_SUPPORTED_TRANSPORTS = {"stdio", "streamable-http"}  # 声明当前支持的 transport 集合，避免散落硬编码。
_COMMON_CONFIG_FIELDS = {"server_id", "name", "description", "enabled", "transport", "timeout_seconds"}  # 声明所有服务都可使用的公共字段。
_STDIO_CONFIG_FIELDS = {"command", "args", "env", "cwd", "startup_timeout_seconds"}  # 声明 stdio 传输允许的专属字段。
_STREAMABLE_HTTP_CONFIG_FIELDS = {"url", "headers", "verify_ssl", "follow_redirects"}  # 声明 streamable-http 传输允许的专属字段。


@dataclass  # 使用数据类描述单个 MCP 服务配置。
class MCPServerConfig:
    """单个 MCP 服务配置。"""

    server_id: str  # 服务唯一标识。
    transport: str  # 传输类型，支持 stdio 与 streamable-http。
    name: str | None = None  # 服务显示名，仅用于展示与日志增强。
    description: str = ""  # 服务说明文本，仅用于展示与日志增强。
    enabled: bool = True  # 是否启用当前服务。
    timeout_seconds: float | None = None  # 通用会话级超时，统一映射到 ClientSession 读超时。
    command: str | None = None  # stdio 模式下的启动命令。
    args: list[str] | None = None  # stdio 模式下的启动参数列表。
    env: dict[str, str] | None = None  # stdio 模式下的附加环境变量。
    cwd: str | None = None  # stdio 模式下的子进程工作目录。
    startup_timeout_seconds: float | None = None  # stdio 模式下的启动与初始化超时。
    url: str | None = None  # streamable-http 模式下的服务地址。
    headers: dict[str, str] | None = None  # streamable-http 模式下的自定义请求头。
    verify_ssl: bool | None = None  # streamable-http 模式下是否校验证书。
    follow_redirects: bool | None = None  # streamable-http 模式下是否跟随重定向。


@dataclass  # 使用数据类保存单个连接的上下文对象。
class MCPConnection:
    """单个 MCP 服务连接上下文。"""

    server_id: str  # 保存服务标识。
    transport_context: Any  # 保存 transport 上下文管理器。
    session_context: Any  # 保存 session 上下文管理器。
    session: Any  # 保存已初始化的 ClientSession 实例。


class _LoopThread:
    """后台事件循环线程。"""

    def __init__(self) -> None:  # 定义初始化方法。
        """启动独立线程并创建事件循环。"""
        self._ready = Event()  # 创建就绪事件，用于等待事件循环创建完成。
        self._loop: asyncio.AbstractEventLoop | None = None  # 初始化事件循环引用。
        self._thread = Thread(target=self._run, name="mcp-client-loop", daemon=True)  # 创建后台守护线程。
        self._thread.start()  # 启动后台线程。
        self._ready.wait()  # 阻塞等待事件循环准备完成。

    def _run(self) -> None:  # 定义线程主函数。
        """在线程内创建并持有事件循环。"""
        loop = asyncio.new_event_loop()  # 创建新的事件循环实例。
        asyncio.set_event_loop(loop)  # 把新事件循环绑定到当前线程。
        self._loop = loop  # 保存事件循环引用。
        self._ready.set()  # 通知外部事件循环已经准备完成。
        loop.run_forever()  # 持续运行事件循环，供跨线程调度任务使用。
        pending = asyncio.all_tasks(loop=loop)  # 收集循环中尚未完成的任务。
        for task in pending:  # 遍历所有未完成任务。
            task.cancel()  # 逐个取消任务，避免线程退出时泄漏协程。
        if pending:  # 判断是否存在待取消任务。
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))  # 等待所有取消动作完成。
        loop.close()  # 关闭事件循环，释放底层资源。

    def run_sync(self, coroutine: Any) -> Any:  # 定义同步执行协程方法。
        """在线程事件循环中同步执行协程并等待结果。"""
        future = self.submit(coroutine)  # 复用统一提交入口，避免重复维护跨线程调度逻辑。
        return future.result()  # 同步等待协程执行完成并返回结果。

    async def run_async(self, coroutine: Any) -> Any:  # 定义异步执行协程方法。
        """在线程事件循环中执行协程，并在当前事件循环里异步等待。"""
        future = self.submit(coroutine)  # 复用统一提交入口，避免重复维护跨线程调度逻辑。
        return await asyncio.wrap_future(future)  # 在当前协程里异步等待跨线程 Future 完成。

    def submit(self, coroutine: Any) -> Future[Any]:  # 定义协程提交方法。
        """把协程提交到后台事件循环，并返回跨线程 Future。"""
        if self._loop is None:  # 判断事件循环是否可用。
            raise RuntimeError("MCP 事件循环尚未初始化")  # 抛出明确错误，避免静默失败。
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)  # 把协程提交到后台事件循环。

    def call_soon(self, callback: Any, *args: Any) -> None:  # 定义线程安全回调调度方法。
        """在线程安全上下文里调度普通回调。"""
        if self._loop is None:  # 判断事件循环是否可用。
            raise RuntimeError("MCP 事件循环尚未初始化")  # 抛出明确错误，避免静默失败。
        self._loop.call_soon_threadsafe(callback, *args)  # 把回调投递到后台事件循环线程执行。

    def stop(self) -> None:  # 定义关闭线程方法。
        """停止后台事件循环并等待线程退出。"""
        if self._loop is not None:  # 判断事件循环是否已经创建。
            self._loop.call_soon_threadsafe(self._loop.stop)  # 在线程安全上下文里请求停止事件循环。
        self._thread.join()  # 等待后台线程完全退出。


class _ManagedStreamableHTTPTransportContext:
    """把 http client 与 streamable-http transport 合并为一个可关闭上下文。"""

    def __init__(self, transport_context: Any, http_client: Any) -> None:  # 定义初始化方法。
        """保存 transport 上下文与需要托管生命周期的 http client。"""
        self._transport_context = transport_context  # 保存原始 transport 上下文。
        self._http_client = http_client  # 保存自建 http client，便于统一关闭。
        self._stack: contextlib.AsyncExitStack | None = None  # 保存关闭栈，确保退出顺序稳定。

    async def __aenter__(self) -> Any:  # 定义异步进入方法。
        """先进入 http client，再进入 transport。"""
        stack = contextlib.AsyncExitStack()  # 创建异步关闭栈。
        if self._http_client is not None:  # 判断是否需要托管自建 http client。
            await stack.enter_async_context(self._http_client)  # 先进入 http client 上下文，确保后续 transport 可复用该 client。
        transport_result = await stack.enter_async_context(self._transport_context)  # 再进入 transport 上下文并获取读写流。
        self._stack = stack  # 保存关闭栈，供 __aexit__ 阶段使用。
        return transport_result  # 返回 transport 进入结果。

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:  # 定义异步退出方法。
        """按 AsyncExitStack 记录的逆序关闭资源。"""
        if self._stack is None:  # 判断是否已经进入过上下文。
            return  # 若未进入则无需执行关闭逻辑。
        await self._stack.aclose()  # 逆序关闭 transport 与 http client。
        self._stack = None  # 清空关闭栈引用，避免重复关闭。


class MCPClientSessionProxy:
    """把后台线程中的 ClientSession 暴露为当前线程可 await 的代理。"""

    def __init__(self, loop_thread: _LoopThread, session: Any) -> None:  # 定义初始化方法。
        """保存后台事件循环线程与真实会话对象。"""
        self._loop_thread = loop_thread  # 保存后台事件循环线程。
        self._session = session  # 保存真实 ClientSession 对象。

    async def call_tool(self, name: str, arguments: dict) -> Any:  # 定义异步调用方法。
        """把远端工具调用转发到后台事件循环中执行。"""
        return await self._loop_thread.run_async(self._session.call_tool(name, arguments=arguments))  # 在线程事件循环中执行真实调用。


class MCPClientManager:
    """MCP 客户端生命周期管理器。"""

    def __init__(self, config_path: str, configs: list[MCPServerConfig]) -> None:  # 定义初始化方法。
        """保存配置路径与已解析的服务配置。"""
        self._config_path = config_path  # 保存配置文件路径，便于报错定位。
        self._configs = configs  # 保存解析后的服务配置列表。
        self._loop_thread: _LoopThread | None = None  # 初始化后台事件循环线程引用。
        self._close_event: asyncio.Event | None = None  # 保存后台生命周期任务的关闭信号。
        self._lifecycle_future: Future[Any] | None = None  # 保存后台生命周期任务句柄，便于关闭阶段等待同一任务退出。
        self._connections: list[MCPConnection] = []  # 初始化连接列表。
        self._tools: list[Tool] = []  # 初始化已发现工具列表。

    @classmethod  # 声明类方法。
    def from_settings(cls, settings: Settings) -> "MCPClientManager":  # 定义从配置构造管理器的方法。
        """从 Settings 读取配置文件并创建管理器。"""
        config_path = Path(settings.mcp_servers_config_path).resolve()  # 解析配置文件绝对路径。
        configs = cls._load_configs(config_path)  # 读取并校验配置文件内容。
        manager = cls(str(config_path), configs)  # 创建管理器实例。
        if any(config.enabled for config in configs):  # 判断是否真的存在启用中的 MCP 服务。
            manager.start()  # 若至少存在一个启用配置，则立即完成启动与发现流程。
        return manager  # 返回已准备好的管理器。

    @staticmethod  # 声明静态方法。
    def _load_configs(config_path: Path) -> list[MCPServerConfig]:  # 定义配置加载方法。
        """解析并校验 MCP 服务配置文件。"""
        if not config_path.exists():  # 判断配置文件是否存在。
            return []  # 配置文件不存在时直接返回空列表，避免带错启动。
        raw_text = config_path.read_text(encoding="utf-8")  # 读取配置文件文本内容。
        normalized_text = MCPClientManager._strip_json_comments(raw_text)  # 先移除 JSONC 风格注释，便于示例配置直接落在同一文件中。
        if not normalized_text.strip():  # 判断文件内容是否为空。
            return []  # 文件内容为空时直接返回空列表，避免解析异常。
        try:  # 尝试解析 JSON。
            raw_data = json.loads(normalized_text)  # 把文本解析为 Python 数据结构。
        except json.JSONDecodeError as exc:  # 捕获 JSON 解析失败异常。
            raise ValueError(f"MCP 配置文件不是合法 JSON: {config_path}") from exc  # 抛出更明确的业务错误。
        if not isinstance(raw_data, list):  # 判断顶层结构是否为列表。
            raise ValueError(f"MCP 配置文件必须是数组: {config_path}")  # 抛出结构错误。
        configs: list[MCPServerConfig] = []  # 初始化配置结果列表。
        for index, item in enumerate(raw_data):  # 遍历每一个服务配置项。
            if not isinstance(item, dict):  # 判断单项是否为对象。
                raise ValueError(f"MCP 配置项必须是对象: index={index}")  # 抛出结构错误。
            configs.append(MCPClientManager._parse_config_item(index=index, item=item))  # 解析并记录单个配置项。
        return configs  # 返回配置对象列表。

    @staticmethod  # 声明静态方法。
    def _parse_config_item(index: int, item: dict[str, Any]) -> MCPServerConfig:  # 定义单项配置解析方法。
        """把原始配置项解析为强类型配置对象。"""
        server_id = MCPClientManager._read_required_string(item=item, key="server_id", index=index)  # 读取服务标识字段。
        transport = item.get("transport")  # 读取传输类型字段。
        if transport not in _SUPPORTED_TRANSPORTS:  # 校验传输类型是否合法。
            raise ValueError(f"MCP 配置 transport 不支持: server_id={server_id}, transport={transport}")  # 抛出字段错误。

        allowed_fields = _COMMON_CONFIG_FIELDS | (_STDIO_CONFIG_FIELDS if transport == "stdio" else _STREAMABLE_HTTP_CONFIG_FIELDS)  # 根据 transport 计算当前合法字段白名单。
        unknown_fields = sorted(set(item.keys()) - allowed_fields)  # 计算所有未知字段，避免继续保持“静默忽略”。
        if unknown_fields:  # 判断是否存在未知字段。
            raise ValueError(f"MCP 配置存在未支持字段: server_id={server_id}, fields={unknown_fields}")  # 启动期直接失败，避免拼写错误被吞掉。

        name = MCPClientManager._read_optional_string(item=item, key="name", server_id=server_id, allow_empty=False)  # 读取可选显示名。
        description = MCPClientManager._read_optional_string(item=item, key="description", server_id=server_id, allow_empty=True) or ""  # 读取可选说明文本。
        enabled = MCPClientManager._read_optional_bool(item=item, key="enabled", server_id=server_id, default=True)  # 读取启用开关，默认启用。
        timeout_seconds = MCPClientManager._read_optional_positive_number(item=item, key="timeout_seconds", server_id=server_id)  # 读取通用会话级超时。

        if transport == "stdio":  # 针对 stdio 传输解析专属字段。
            command = MCPClientManager._read_required_string(item=item, key="command", index=index, server_id=server_id)  # 读取必填启动命令。
            args = MCPClientManager._read_optional_string_list(item=item, key="args", server_id=server_id)  # 读取可选参数列表。
            env = MCPClientManager._read_optional_string_dict(item=item, key="env", server_id=server_id)  # 读取可选环境变量字典。
            cwd = MCPClientManager._read_optional_string(item=item, key="cwd", server_id=server_id, allow_empty=False)  # 读取可选工作目录。
            startup_timeout_seconds = MCPClientManager._read_optional_positive_number(item=item, key="startup_timeout_seconds", server_id=server_id)  # 读取启动超时。
            return MCPServerConfig(  # 构造 stdio 配置对象。
                server_id=server_id,  # 保存服务标识。
                transport=transport,  # 保存传输类型。
                name=name,  # 保存显示名。
                description=description,  # 保存说明文本。
                enabled=enabled,  # 保存启用开关。
                timeout_seconds=timeout_seconds,  # 保存通用会话超时。
                command=command,  # 保存启动命令。
                args=args,  # 保存参数列表。
                env=env,  # 保存环境变量字典。
                cwd=cwd,  # 保存工作目录。
                startup_timeout_seconds=startup_timeout_seconds,  # 保存启动超时。
            )

        url = MCPClientManager._read_required_string(item=item, key="url", index=index, server_id=server_id)  # 读取必填 URL。
        headers = MCPClientManager._read_optional_string_dict(item=item, key="headers", server_id=server_id)  # 读取自定义请求头。
        verify_ssl = MCPClientManager._read_optional_bool(item=item, key="verify_ssl", server_id=server_id, default=None)  # 读取证书校验开关。
        follow_redirects = MCPClientManager._read_optional_bool(item=item, key="follow_redirects", server_id=server_id, default=None)  # 读取重定向开关。
        return MCPServerConfig(  # 构造 streamable-http 配置对象。
            server_id=server_id,  # 保存服务标识。
            transport=transport,  # 保存传输类型。
            name=name,  # 保存显示名。
            description=description,  # 保存说明文本。
            enabled=enabled,  # 保存启用开关。
            timeout_seconds=timeout_seconds,  # 保存通用会话超时。
            url=url,  # 保存服务 URL。
            headers=headers,  # 保存自定义请求头。
            verify_ssl=verify_ssl,  # 保存证书校验开关。
            follow_redirects=follow_redirects,  # 保存重定向开关。
        )

    @staticmethod  # 声明静态方法。
    def _strip_json_comments(raw_text: str) -> str:  # 定义注释剥离方法。
        """移除 JSONC 风格的行注释与块注释。"""
        result: list[str] = []  # 初始化结果字符列表。
        in_string = False  # 标记当前是否处于字符串内部。
        escaped = False  # 标记当前字符是否处于转义状态。
        in_line_comment = False  # 标记当前是否处于 `//` 行注释中。
        in_block_comment = False  # 标记当前是否处于 `/* */` 块注释中。
        index = 0  # 初始化扫描游标。
        text_length = len(raw_text)  # 记录文本长度，避免循环里重复计算。

        while index < text_length:  # 逐字符扫描整段文本。
            current = raw_text[index]  # 读取当前字符。
            next_char = raw_text[index + 1] if index + 1 < text_length else ""  # 安全读取后继字符。

            if in_line_comment:  # 若当前位于行注释中。
                if current == "\n":  # 直到遇到换行才结束当前行注释。
                    in_line_comment = False  # 清除行注释标记。
                    result.append(current)  # 仍保留换行，避免影响错误定位和文件可读性。
                index += 1  # 推进游标。
                continue  # 继续处理后续字符。

            if in_block_comment:  # 若当前位于块注释中。
                if current == "*" and next_char == "/":  # 检测块注释结束标记。
                    in_block_comment = False  # 清除块注释标记。
                    index += 2  # 跳过结束标记两个字符。
                    continue  # 继续处理后续字符。
                if current == "\n":  # 块注释中的换行需要保留。
                    result.append(current)  # 保留换行，避免文件结构全部挤到一行。
                index += 1  # 推进游标。
                continue  # 继续处理后续字符。

            if in_string:  # 若当前位于字符串内部。
                result.append(current)  # 字符串中的任何字符都原样保留。
                if escaped:  # 如果上一字符是转义符。
                    escaped = False  # 当前字符仅用于转义，不改变字符串状态。
                elif current == "\\":  # 检测新的转义起始符。
                    escaped = True  # 标记下一字符被转义。
                elif current == '"':  # 检测字符串结束引号。
                    in_string = False  # 结束字符串模式。
                index += 1  # 推进游标。
                continue  # 继续处理后续字符。

            if current == '"':  # 检测字符串起始引号。
                in_string = True  # 进入字符串模式。
                result.append(current)  # 保留引号字符。
                index += 1  # 推进游标。
                continue  # 继续处理后续字符。

            if current == "/" and next_char == "/":  # 检测行注释起始标记。
                in_line_comment = True  # 进入行注释模式。
                index += 2  # 跳过起始标记。
                continue  # 继续处理后续字符。

            if current == "/" and next_char == "*":  # 检测块注释起始标记。
                in_block_comment = True  # 进入块注释模式。
                index += 2  # 跳过起始标记。
                continue  # 继续处理后续字符。

            result.append(current)  # 对普通字符直接保留。
            index += 1  # 推进游标。

        return "".join(result)  # 拼接剥离注释后的最终文本。

    @staticmethod  # 声明静态方法。
    def _read_required_string(item: dict[str, Any], key: str, index: int, server_id: str | None = None) -> str:  # 定义必填字符串读取方法。
        """读取必填非空字符串字段。"""
        value = item.get(key)  # 读取目标字段。
        if isinstance(value, str) and value:  # 校验字段是否为非空字符串。
            return value  # 返回已校验通过的值。
        if key == "server_id":  # 对 server_id 缺失场景使用更明确错误文案。
            raise ValueError(f"MCP 配置缺少有效 server_id: index={index}")  # 抛出字段错误。
        raise ValueError(f"MCP 配置缺少有效 {key}: server_id={server_id}")  # 对其他字段抛出统一错误。

    @staticmethod  # 声明静态方法。
    def _read_optional_string(item: dict[str, Any], key: str, server_id: str, allow_empty: bool) -> str | None:  # 定义可选字符串读取方法。
        """读取可选字符串字段。"""
        if key not in item:  # 判断字段是否存在。
            return None  # 缺失时直接返回 None。
        value = item[key]  # 读取目标字段。
        if not isinstance(value, str):  # 校验字段类型。
            raise ValueError(f"MCP 配置字段必须是字符串: server_id={server_id}, field={key}")  # 抛出类型错误。
        if not allow_empty and not value:  # 若不允许空字符串，则进一步校验内容。
            raise ValueError(f"MCP 配置字段不能为空字符串: server_id={server_id}, field={key}")  # 抛出取值错误。
        return value  # 返回已校验通过的值。

    @staticmethod  # 声明静态方法。
    def _read_optional_bool(item: dict[str, Any], key: str, server_id: str, default: bool | None) -> bool | None:  # 定义可选布尔读取方法。
        """读取可选布尔字段。"""
        if key not in item:  # 判断字段是否存在。
            return default  # 缺失时返回调用方传入的默认值。
        value = item[key]  # 读取目标字段。
        if not isinstance(value, bool):  # 校验字段类型。
            raise ValueError(f"MCP 配置字段必须是布尔值: server_id={server_id}, field={key}")  # 抛出类型错误。
        return value  # 返回已校验通过的值。

    @staticmethod  # 声明静态方法。
    def _read_optional_positive_number(item: dict[str, Any], key: str, server_id: str) -> float | None:  # 定义可选正数读取方法。
        """读取可选正数字段。"""
        if key not in item:  # 判断字段是否存在。
            return None  # 缺失时返回 None。
        value = item[key]  # 读取目标字段。
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:  # 校验值是否为正数，同时排除 bool 被当作 int 的情况。
            raise ValueError(f"MCP 配置字段必须是正数: server_id={server_id}, field={key}")  # 抛出类型或取值错误。
        return float(value)  # 统一转成 float，避免后续处理分支。

    @staticmethod  # 声明静态方法。
    def _read_optional_string_list(item: dict[str, Any], key: str, server_id: str) -> list[str] | None:  # 定义可选字符串列表读取方法。
        """读取可选字符串列表字段。"""
        if key not in item:  # 判断字段是否存在。
            return None  # 缺失时返回 None。
        value = item[key]  # 读取目标字段。
        if not isinstance(value, list) or any(not isinstance(entry, str) for entry in value):  # 校验列表结构与元素类型。
            raise ValueError(f"MCP 配置字段必须是字符串数组: server_id={server_id}, field={key}")  # 抛出类型错误。
        return value  # 返回已校验通过的值。

    @staticmethod  # 声明静态方法。
    def _read_optional_string_dict(item: dict[str, Any], key: str, server_id: str) -> dict[str, str] | None:  # 定义可选字符串字典读取方法。
        """读取可选字符串字典字段。"""
        if key not in item:  # 判断字段是否存在。
            return None  # 缺失时返回 None。
        value = item[key]  # 读取目标字段。
        if not isinstance(value, dict):  # 校验字段类型。
            raise ValueError(f"MCP 配置字段必须是对象: server_id={server_id}, field={key}")  # 抛出类型错误。
        if any(not isinstance(dict_key, str) or not isinstance(dict_value, str) for dict_key, dict_value in value.items()):  # 校验对象中的键和值都必须是字符串。
            raise ValueError(f"MCP 配置字段必须是字符串键值对对象: server_id={server_id}, field={key}")  # 抛出结构错误。
        return value  # 返回已校验通过的值。

    def start(self) -> None:  # 定义启动方法。
        """启动后台事件循环并完成 MCP 服务发现。"""
        if self._loop_thread is not None:  # 判断管理器是否已经启动。
            return  # 若已启动则直接返回，避免重复初始化。
        self._loop_thread = _LoopThread()  # 创建后台事件循环线程。
        try:  # 尝试在后台线程中建立所有远端连接。
            started_future: Future[None] = Future()  # 创建跨线程启动结果句柄，用于等待生命周期任务完成初始化。
            self._lifecycle_future = self._loop_thread.submit(self._run_lifecycle(started_future))  # 启动长期存活的生命周期任务，统一持有连接进入与退出。
            started_future.result()  # 同步等待生命周期任务完成连接建立与工具发现。
        except Exception:  # 捕获启动过程中的任意异常。
            if self._lifecycle_future is not None:  # 判断是否已经创建生命周期任务。
                self._lifecycle_future.cancel()  # 尝试取消失败的生命周期任务，避免后台遗留悬空协程。
                self._lifecycle_future = None  # 清空生命周期任务引用，避免保留失败状态。
            self._loop_thread.stop()  # 启动失败时先关闭后台线程。
            self._loop_thread = None  # 清空线程引用，避免留下半初始化状态。
            raise  # 继续向上抛出异常，让容器创建失败。

    async def _run_lifecycle(self, started_future: Future[None]) -> None:  # 定义长期生命周期任务。
        """在同一个后台任务里完成连接建立、持有与关闭。"""
        self._close_event = asyncio.Event()  # 创建关闭信号，供外部关闭阶段唤醒当前生命周期任务。
        try:  # 尝试完成所有连接建立与工具发现。
            for config in self._configs:  # 遍历所有服务配置。
                if not config.enabled:  # 对显式禁用的配置直接跳过。
                    continue  # 不建立连接，也不注册工具。
                connection = await self._open_connection(config)  # 打开单个服务连接。
                self._connections.append(connection)  # 保存连接上下文，供同一生命周期任务关闭时复用。
                tool_items = await self._list_all_tool_items(connection.session)  # 沿着 nextCursor 拉取当前服务暴露的全量工具列表。
                session_proxy = MCPClientSessionProxy(self._loop_thread, connection.session)  # 为当前服务创建可跨线程 await 的会话代理。
                for tool_item in tool_items:  # 遍历服务返回的每一个工具描述。
                    remote_tool_name = self._get_remote_tool_name(tool_item)  # 提取远端工具原始名称，而不是本地前缀名。
                    description = self._get_tool_description(tool_item)  # 提取工具描述。
                    input_schema = self._get_tool_input_schema(tool_item)  # 提取输入模式。
                    self._tools.append(  # 把发现到的远端工具适配为本地 Tool。
                        MCPToolAdapter(  # 构造 MCP 工具适配器。
                            server_id=config.server_id,  # 记录来源服务标识。
                            remote_tool_name=remote_tool_name,  # 记录远端原始工具名称。
                            description=description,  # 保存工具描述。
                            input_schema=input_schema,  # 保存输入参数模式。
                            session=session_proxy,  # 注入会话代理，供运行期跨线程调用。
                        )  # 结束工具适配器构造。
                    )
            started_future.set_result(None)  # 通知启动方：连接建立完成，可以继续创建容器。
            await self._close_event.wait()  # 在同一任务内持续持有上下文，直到外部显式发出关闭信号。
        except BaseException as exc:  # 捕获生命周期任务中的任意异常。
            if not started_future.done():  # 判断启动结果是否尚未返回给调用方。
                started_future.set_exception(exc)  # 把启动异常同步给创建线程，阻止应用带错启动。
            raise  # 继续向上抛出异常，让生命周期 Future 保持失败态。
        finally:  # 无论正常关闭还是异常失败，都在同一任务内释放上下文。
            try:  # 尝试执行同任务关闭，避免 SDK 的 TaskGroup 跨任务退出。
                await self._async_close()  # 在当前生命周期任务内逆序释放所有已打开连接。
            finally:  # 收尾清理生命周期状态。
                self._close_event = None  # 清空关闭信号引用，避免外部继续误用旧对象。

    async def _list_all_tool_items(self, session: Any) -> list[Any]:  # 定义分页拉全辅助方法。
        """沿着 nextCursor 拉取某个 MCP 服务的全量工具描述。"""
        tool_items: list[Any] = []  # 初始化全量工具描述列表。
        cursor: str | None = None  # 初始化分页游标，None 表示从首页开始。
        while True:  # 循环拉取，直到服务端不再返回 nextCursor。
            tools_response = await session.list_tools(cursor=cursor)  # 按当前游标拉取一页工具列表。
            page_tools = getattr(tools_response, "tools", [])  # 读取当前页工具列表字段。
            if isinstance(page_tools, list):  # 判断当前页工具字段是否为列表。
                tool_items.extend(page_tools)  # 把当前页工具追加到全量结果中。
            next_cursor = getattr(tools_response, "nextCursor", None)  # 读取下一页游标字段。
            if not isinstance(next_cursor, str) or not next_cursor:  # 判断是否已经没有下一页可拉。
                break  # 没有下一页时结束循环。
            cursor = next_cursor  # 更新游标，继续拉取后续页面。
        return tool_items  # 返回已拉全的工具描述列表。

    async def _open_connection(self, config: MCPServerConfig) -> MCPConnection:  # 定义单连接打开方法。
        """根据配置打开单个 MCP 服务连接。"""
        mcp_module = self._import_module("mcp")  # 动态导入 mcp 主模块。
        client_session_cls = getattr(mcp_module, "ClientSession")  # 读取 ClientSession 类型。
        if config.transport == "stdio":  # 判断是否为 stdio 传输。
            return await self._open_stdio_connection(config=config, mcp_module=mcp_module, client_session_cls=client_session_cls)  # 打开 stdio 连接。
        return await self._open_streamable_http_connection(config=config, client_session_cls=client_session_cls)  # 打开 streamable-http 连接。

    async def _open_stdio_connection(self, config: MCPServerConfig, mcp_module: Any, client_session_cls: Any) -> MCPConnection:  # 定义 stdio 打开方法。
        """根据配置打开单个 stdio MCP 服务连接。"""
        stdio_module = self._import_module("mcp.client.stdio")  # 动态导入 stdio 客户端模块。
        server_parameters_cls = getattr(mcp_module, "StdioServerParameters")  # 读取 stdio 参数类型。

        async def _open_and_initialize() -> MCPConnection:  # 定义内部打开协程，便于统一套启动超时。
            transport_context = None  # 初始化 transport 上下文引用。
            session_context = None  # 初始化 session 上下文引用。
            try:  # 尝试完成 transport 与 session 初始化。
                transport_context = stdio_module.stdio_client(  # 构造 stdio transport 上下文。
                    server_parameters_cls(  # 构造 stdio 服务参数。
                        command=config.command,  # 传入启动命令。
                        args=config.args or [],  # 传入启动参数列表。
                        env=config.env,  # 传入环境变量字典。
                        cwd=config.cwd,  # 传入工作目录。
                    )  # 结束参数对象构造。
                )
                read_stream, write_stream = await transport_context.__aenter__()  # 进入 transport 上下文并获取读写流。
                session_context = self._build_session_context(client_session_cls=client_session_cls, read_stream=read_stream, write_stream=write_stream, config=config)  # 构造会话上下文。
                session = await session_context.__aenter__()  # 进入 session 上下文并获取真实会话对象。
                await session.initialize()  # 按官方 SDK 生命周期先执行 initialize。
                return MCPConnection(  # 返回连接上下文载体。
                    server_id=config.server_id,  # 记录服务标识。
                    transport_context=transport_context,  # 保存 transport 上下文。
                    session_context=session_context,  # 保存 session 上下文。
                    session=session,  # 保存真实会话实例。
                )
            except BaseException:  # 捕获初始化阶段异常与取消，确保已进入资源能够被回收。
                if session_context is not None:  # 若 session 已进入，则先关闭 session。
                    await session_context.__aexit__(None, None, None)  # 关闭已进入的 session 上下文。
                if transport_context is not None:  # 若 transport 已进入，则继续关闭 transport。
                    await transport_context.__aexit__(None, None, None)  # 关闭已进入的 transport 上下文。
                raise  # 继续向上抛出异常，让启动阶段失败。

        try:  # 尝试按配置建立连接。
            if config.startup_timeout_seconds is None:  # 若未配置启动超时，则直接执行。
                return await _open_and_initialize()  # 返回建立完成的连接。
            return await asyncio.wait_for(_open_and_initialize(), timeout=config.startup_timeout_seconds)  # 按配置超时包裹启动阶段。
        except TimeoutError as exc:  # 捕获启动超时异常。
            raise TimeoutError(f"MCP stdio 服务启动超时: server_id={config.server_id}, timeout_seconds={config.startup_timeout_seconds}") from exc  # 抛出更明确的业务错误。

    async def _open_streamable_http_connection(self, config: MCPServerConfig, client_session_cls: Any) -> MCPConnection:  # 定义 streamable-http 打开方法。
        """根据配置打开单个 streamable-http MCP 服务连接。"""
        http_module = self._import_module("mcp.client.streamable_http")  # 动态导入 HTTP 客户端模块。
        transport_context = None  # 初始化 transport 上下文引用。
        session_context = None  # 初始化 session 上下文引用。
        try:  # 尝试完成 transport 与 session 初始化。
            http_client = self._create_streamable_http_client(config)  # 按配置创建自定义 httpx.AsyncClient。
            transport_context = _ManagedStreamableHTTPTransportContext(  # 组合 transport 与 http client 生命周期。
                transport_context=http_module.streamable_http_client(config.url, http_client=http_client),  # 构造 streamable-http transport 上下文。
                http_client=http_client,  # 注入自建 http client，供 transport 复用。
            )
            transport_result = await transport_context.__aenter__()  # 进入 transport 上下文并获取返回值。
            read_stream, write_stream = transport_result[0], transport_result[1]  # 只提取读写流，忽略附加返回值。
            session_context = self._build_session_context(client_session_cls=client_session_cls, read_stream=read_stream, write_stream=write_stream, config=config)  # 构造会话上下文。
            session = await session_context.__aenter__()  # 进入 session 上下文并获取真实会话对象。
            await session.initialize()  # 按官方 SDK 生命周期先执行 initialize。
            return MCPConnection(  # 返回连接上下文载体。
                server_id=config.server_id,  # 记录服务标识。
                transport_context=transport_context,  # 保存 transport 上下文。
                session_context=session_context,  # 保存 session 上下文。
                session=session,  # 保存真实会话实例。
            )
        except BaseException:  # 捕获会话初始化失败异常。
            if session_context is not None:  # 若 session 已进入，则先关闭 session。
                await session_context.__aexit__(None, None, None)  # 关闭已进入的 session 上下文。
            if transport_context is not None:  # 若 transport 已进入，则继续关闭 transport 与 http client。
                await transport_context.__aexit__(None, None, None)  # 关闭 transport 与 http client。
            raise  # 继续向上抛出异常，阻止应用带错启动。

    def _build_session_context(self, client_session_cls: Any, read_stream: Any, write_stream: Any, config: MCPServerConfig) -> Any:  # 定义会话上下文构造方法。
        """按配置构造 ClientSession 上下文。"""
        read_timeout_seconds = timedelta(seconds=config.timeout_seconds) if config.timeout_seconds is not None else None  # 把秒数配置转换为 SDK 需要的 timedelta。
        return client_session_cls(  # 构造 ClientSession 上下文对象。
            read_stream,  # 传入读流。
            write_stream,  # 传入写流。
            read_timeout_seconds=read_timeout_seconds,  # 注入通用会话级读超时。
        )

    def _create_streamable_http_client(self, config: MCPServerConfig) -> Any:  # 定义 HTTP client 构造方法。
        """根据配置创建专供 streamable-http 使用的 httpx.AsyncClient。"""
        httpx_module = self._import_module("httpx")  # 动态导入 httpx，避免模块导入时过早建立依赖。
        httpx_utils_module = self._import_module("mcp.shared._httpx_utils")  # 动态导入 MCP SDK 的默认 HTTP 配置常量。
        default_timeout_seconds = float(getattr(httpx_utils_module, "MCP_DEFAULT_TIMEOUT", 30.0))  # 读取 MCP SDK 默认普通请求超时。
        default_sse_read_timeout_seconds = float(getattr(httpx_utils_module, "MCP_DEFAULT_SSE_READ_TIMEOUT", 300.0))  # 读取 MCP SDK 默认 SSE 读取超时。
        timeout_seconds = config.timeout_seconds if config.timeout_seconds is not None else default_timeout_seconds  # 仅覆盖普通请求超时，SSE 读取超时仍保留官方默认值。
        timeout = httpx_module.Timeout(timeout_seconds, read=default_sse_read_timeout_seconds)  # 构造 httpx 超时对象。
        client_kwargs: dict[str, Any] = {  # 初始化 AsyncClient 构造参数。
            "timeout": timeout,  # 注入请求超时配置。
            "verify": config.verify_ssl if config.verify_ssl is not None else True,  # 注入证书校验开关，缺省仍保持安全默认值。
            "follow_redirects": config.follow_redirects if config.follow_redirects is not None else True,  # 注入重定向开关，缺省与 MCP SDK 默认行为一致。
        }
        if config.headers is not None:  # 判断是否配置了自定义请求头。
            client_kwargs["headers"] = config.headers  # 注入自定义请求头字典。
        return httpx_module.AsyncClient(**client_kwargs)  # 返回已按配置构造的 AsyncClient。

    def list_tools(self) -> list[Tool]:  # 定义列出工具方法。
        """返回发现到的 MCP 工具列表。"""
        return list(self._tools)  # 返回工具副本，避免外部直接修改内部列表。

    async def aclose(self) -> None:  # 定义异步关闭方法。
        """关闭所有 MCP 连接并停止后台线程。"""
        if self._loop_thread is None:  # 判断是否存在后台线程。
            return  # 若从未启动，则无需做任何清理。
        try:  # 尝试先等待生命周期任务在同一任务内完成关闭。
            if self._close_event is not None:  # 判断生命周期任务是否已经进入等待关闭状态。
                self._loop_thread.call_soon(self._close_event.set)  # 在线程安全上下文里触发关闭信号，唤醒生命周期任务执行同任务退出。
            if self._lifecycle_future is not None:  # 判断是否存在生命周期任务句柄。
                await asyncio.wrap_future(self._lifecycle_future)  # 等待生命周期任务完整退出，确保资源全部释放。
        finally:  # 无论关闭过程是否成功，都要停止后台线程并清理引用。
            self._loop_thread.stop()  # 关闭后台线程。
            self._loop_thread = None  # 清空线程引用。
            self._lifecycle_future = None  # 清空生命周期任务句柄，避免保留旧状态。

    async def _async_close(self) -> None:  # 定义异步关闭实现。
        """在后台事件循环里关闭所有连接上下文。"""
        for connection in reversed(self._connections):  # 按逆序关闭所有连接，保证资源释放顺序正确。
            try:  # 尝试关闭 session 上下文。
                await connection.session_context.__aexit__(None, None, None)  # 关闭 session 上下文。
            finally:  # 无论 session 关闭是否报错，都继续关闭 transport。
                await connection.transport_context.__aexit__(None, None, None)  # 关闭 transport 上下文。
        self._connections.clear()  # 清空连接缓存列表。
        self._tools.clear()  # 清空工具缓存列表。

    def _get_remote_tool_name(self, tool_item: Any) -> str:  # 定义工具名提取方法。
        """从 SDK tool 描述中提取远端原始名称。"""
        name = getattr(tool_item, "name", None)  # 优先按属性读取工具名。
        if not isinstance(name, str) or not name:  # 校验工具名是否合法。
            raise ValueError(f"MCP tool 缺少有效 name: config={self._config_path}")  # 抛出结构错误。
        return name  # 返回远端原始工具名，后续本地暴露名会在适配器层统一补 `mcp_` 前缀。

    def _get_tool_description(self, tool_item: Any) -> str:  # 定义描述提取方法。
        """从 SDK tool 描述中提取描述。"""
        description = getattr(tool_item, "description", "")  # 优先按属性读取描述字段。
        return description if isinstance(description, str) else ""  # 对非字符串描述做空字符串兜底。

    def _get_tool_input_schema(self, tool_item: Any) -> dict[str, Any]:  # 定义输入模式提取方法。
        """从 SDK tool 描述中提取输入模式。"""
        input_schema = getattr(tool_item, "inputSchema", None)  # 优先读取官方常见字段名。
        if input_schema is None:  # 判断是否未取到值。
            input_schema = getattr(tool_item, "input_schema", None)  # 兼容 snake_case 字段名。
        return input_schema if isinstance(input_schema, dict) else {"type": "object"}  # 对缺省值做最小对象模式兜底。

    def _import_module(self, module_name: str) -> Any:  # 定义动态导入辅助方法。
        """动态导入模块，并在缺依赖时给出更明确错误。"""
        try:  # 尝试导入目标模块。
            return importlib.import_module(module_name)  # 返回成功导入的模块对象。
        except ModuleNotFoundError as exc:  # 捕获缺依赖错误。
            raise ModuleNotFoundError("缺少官方 MCP Python SDK，请先安装 `mcp` 依赖") from exc  # 抛出更明确的依赖提示。
