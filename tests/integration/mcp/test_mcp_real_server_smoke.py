"""MCP 真实 Server 端到端冒烟测试。

重点验证三个生命周期风险点：
1. stdio server 长时间空闲后还能否正常 call_tool
2. 应用关闭时 session/transport 是否干净退出
3. 多个 server 并存时是否有线程退出残留

说明：
- 本测试直接读取项目根目录的 mcp_servers.json，连接其中已启用的真实 MCP 服务。
- 不使用 psutil，仅依赖标准库（subprocess + threading + ps）检查进程和线程。
- 因涉及真实网络和子进程启动，标记为 smoke 测试，CI 中可按需运行。
"""

from __future__ import annotations  # 启用未来注解，避免前向引用问题。

import asyncio  # 导入 asyncio，用于异步休眠和超时控制。
import subprocess  # 导入 subprocess，用于通过 ps 命令查找进程。
import threading  # 导入 threading，用于枚举和断言线程残留。
from pathlib import Path  # 导入 Path，用于定位项目根目录的 mcp_servers.json。

import pytest  # 导入 pytest 测试框架。

from app.infra.tools.mcp_client_manager import MCPClientManager, MCPServerConfig  # 导入被测 MCP 客户端管理器。


def _project_root() -> Path:
    """返回项目根目录路径（tests/integration/mcp 上溯 3 级）。"""
    return Path(__file__).resolve().parents[3]


def _load_mcp_configs() -> list[MCPServerConfig]:
    """加载项目根目录下的 mcp_servers.json 配置。

    若文件不存在则跳过测试。
    """
    config_path = _project_root() / "mcp_servers.json"
    if not config_path.exists():
        pytest.skip(f"缺少 MCP 配置文件: {config_path}")
    return MCPClientManager._load_configs(config_path)  # type: ignore[attr-defined]


def _get_stdio_server_config() -> MCPServerConfig:
    """获取启用的 stdio 服务配置。"""
    configs = _load_mcp_configs()
    for cfg in configs:
        if cfg.enabled and cfg.transport == "stdio":
            return cfg
    pytest.skip("当前 mcp_servers.json 中未启用任何 stdio server，跳过 stdio 相关冒烟测试")


def _get_http_server_config() -> MCPServerConfig | None:
    """获取启用的 streamable-http 服务配置（可选）。"""
    configs = _load_mcp_configs()
    for cfg in configs:
        if cfg.enabled and cfg.transport == "streamable-http":
            return cfg
    return None


def _stdio_pattern_for_config(cfg: MCPServerConfig) -> str:
    """从 stdio 配置中提取一个可用于 `ps aux` 匹配的特征字符串。

    优先取 args 中首个非选项参数（如 @upstash/context7-mcp@latest），
    若 args 为空则回退到 command（如 npx）。
    """
    if cfg.args:
        for arg in cfg.args:
            if not arg.startswith("-"):
                return arg
    return cfg.command or "npx"


def _find_pids_by_cmd_pattern(pattern: str) -> list[int]:
    """通过 `ps aux` 查找命令行中包含指定特征的进程 PID 列表。

    不使用 psutil，仅依赖标准库 subprocess + ps。
    会自动排除 grep、pytest 自身等干扰进程。
    """
    result = subprocess.run(
        ["ps", "aux"],
        capture_output=True,
        text=True,
        check=False,
    )
    pids: list[int] = []
    for line in result.stdout.splitlines():
        if pattern not in line:
            continue
        # 排除 grep 以及 pytest 主进程自身的 grep 行为
        if "grep" in line or "python -m pytest" in line or "pytest -k" in line:
            continue
        parts = line.split()
        if len(parts) > 1:
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


def _mcp_threads() -> list[threading.Thread]:
    """返回所有名称包含 `mcp-client-loop` 的线程。"""
    return [t for t in threading.enumerate() if "mcp-client-loop" in t.name]


# ============================================================================
# 测试 1：stdio server 长时间空闲后还能否正常 call_tool
# ============================================================================
@pytest.mark.smoke
@pytest.mark.asyncio
async def test_stdio_server_call_tool_after_long_idle() -> None:
    """验证 stdio server 在长时间空闲后，底层 session 调用仍能正常工作。

    这是为了覆盖 stdio transport 底层 stdin/stdout 管道
    在长时间空闲后可能发生 BrokenPipeError / EOFError 的风险。
    """
    stdio_cfg = _get_stdio_server_config()
    manager = MCPClientManager(
        config_path="unused.json",
        configs=[stdio_cfg],
    )
    manager.start()

    assert len(manager._connections) == 1, "stdio server 应建立唯一连接"
    session = manager._connections[0].session

    # 第一次调用：通过后台线程直接调用 session.list_tools() 验证连接 alive
    first_result = await manager._loop_thread.run_async(session.list_tools())
    assert first_result is not None, "首次 list_tools 不应返回 None"

    # 空闲等待 60 秒，模拟用户长时间未发请求的窗口
    await asyncio.sleep(60)

    # 第二次调用：再次验证 session 未被底层管道断开
    second_result = await manager._loop_thread.run_async(session.list_tools())
    assert second_result is not None, "空闲 60 秒后 list_tools 仍应成功"

    await manager.aclose()


# ============================================================================
# 测试 2：应用关闭时 session/transport 是否干净退出
# ============================================================================
@pytest.mark.smoke
@pytest.mark.asyncio
async def test_stdio_server_clean_shutdown() -> None:
    """验证关闭 MCPClientManager 后，stdio 子进程和后台线程都能完全释放。

    这是为了避免子进程僵尸化或后台守护线程泄漏。
    """
    stdio_cfg = _get_stdio_server_config()
    pattern = _stdio_pattern_for_config(stdio_cfg)

    before_threads = _mcp_threads()
    before_pids = _find_pids_by_cmd_pattern(pattern)

    manager = MCPClientManager(config_path="unused.json", configs=[stdio_cfg])
    manager.start()

    # 启动后应出现新的 stdio 子进程
    after_pids = _find_pids_by_cmd_pattern(pattern)
    new_pids = [pid for pid in after_pids if pid not in before_pids]
    assert len(new_pids) > 0, f"启动后应出现包含 '{pattern}' 的 stdio 子进程"

    # 启动后应新增一个 mcp-client-loop 线程
    after_threads = _mcp_threads()
    assert len(after_threads) == len(before_threads) + 1, "启动后应新增一个 mcp-client-loop 线程"

    await manager.aclose()

    # 验证线程已消失
    final_threads = _mcp_threads()
    assert len(final_threads) == len(before_threads), "关闭后 mcp-client-loop 线程应消失"

    # 验证 stdio 子进程已消失
    final_pids = _find_pids_by_cmd_pattern(pattern)
    leaked = [pid for pid in new_pids if pid in final_pids]
    assert len(leaked) == 0, f"关闭后不应残留 stdio 子进程，残留 PIDs: {leaked}"


# ============================================================================
# 测试 3：多个 server 并存时是否有线程退出残留
# ============================================================================
@pytest.mark.smoke
@pytest.mark.asyncio
async def test_multiple_servers_no_thread_leak() -> None:
    """验证同时连接多个 MCP server（stdio + http）时，只产生一个后台线程，
    且关闭后该线程完全退出，无任何残留。
    """
    configs = _load_mcp_configs()
    enabled = [c for c in configs if c.enabled]
    if len(enabled) < 2:
        pytest.skip("当前配置中启用的 server 不足 2 个，跳过多 server 线程泄漏测试")

    before_threads = _mcp_threads()

    # 在启动前记录所有 stdio server 子进程基线，便于后续计算新增 PID
    stdio_before_pids: dict[str, list[int]] = {}
    for cfg in enabled:
        if cfg.transport == "stdio":
            pattern = _stdio_pattern_for_config(cfg)
            stdio_before_pids[cfg.server_id] = _find_pids_by_cmd_pattern(pattern)

    manager = MCPClientManager(config_path="unused.json", configs=enabled)
    manager.start()

    # 当前实现是在单一个 _LoopThread 中托管所有连接
    after_threads = _mcp_threads()
    assert len(after_threads) == len(before_threads) + 1, "多 server 并存时只应新增一个 mcp-client-loop 线程"

    # 记录各 stdio server 启动后的新增子进程 PID
    stdio_new_pids: dict[str, list[int]] = {}
    for cfg in enabled:
        if cfg.transport == "stdio":
            pattern = _stdio_pattern_for_config(cfg)
            after_pids = _find_pids_by_cmd_pattern(pattern)
            stdio_new_pids[cfg.server_id] = [
                pid for pid in after_pids if pid not in stdio_before_pids[cfg.server_id]
            ]
            assert len(stdio_new_pids[cfg.server_id]) > 0, (
                f"stdio server {cfg.server_id} 启动后应存在子进程"
            )

    await manager.aclose()

    # 验证后台线程已消失
    final_threads = _mcp_threads()
    assert len(final_threads) == len(before_threads), "关闭后 mcp-client-loop 线程应消失"

    # 验证所有 stdio 子进程已消失
    for cfg in enabled:
        if cfg.transport == "stdio":
            pattern = _stdio_pattern_for_config(cfg)
            final_pids = _find_pids_by_cmd_pattern(pattern)
            leaked = [pid for pid in stdio_new_pids[cfg.server_id] if pid in final_pids]
            assert len(leaked) == 0, (
                f"关闭后 server {cfg.server_id} 不应残留子进程，残留 PIDs: {leaked}"
            )
