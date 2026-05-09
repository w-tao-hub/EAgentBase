"""项目内 Python 脚本执行工具实现。

提供 RunPythonScriptTool，用于在当前项目的虚拟环境 .venv/bin/python 中
执行 workspace_root 下已有的 .py 脚本，并安全地透传参数与结果。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Dict

from app.core.models.tool import Tool, ToolResult

# 默认超时时间（秒），v1 使用固定常量
DEFAULT_TIMEOUT_SECONDS = 60


class RunPythonScriptTool(Tool):
    """在项目虚拟环境中执行已有 Python 脚本。"""

    def __init__(self, workspace_root: str) -> None:
        """初始化工具实例。

        Args:
            workspace_root: 工作区根目录绝对或相对路径，脚本必须位于该目录内。
        """
        self._workspace_root = workspace_root  # 保存工作区根目录

    @property
    def name(self) -> str:
        """工具标识符。"""
        return "run_python_script"

    @property
    def description(self) -> str:
        """工具描述。"""
        return (
            "在项目虚拟环境 .venv/bin/python 中执行 workspace_root 下已有的 .py 脚本。"
            "仅支持相对路径，禁止越级访问项目外文件。"
        )

    @property
    def input_schema(self) -> Dict[str, Any]:
        """JSON Schema 输入参数定义。"""
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "script_path": {
                    "description": "项目内待执行脚本的相对路径（例如 scripts/demo.py）",
                    "type": "string",
                },
                "args": {
                    "description": "传给脚本的命令行参数数组，每项对应一个 argv 元素",
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                },
            },
            "required": ["script_path"],
            "additionalProperties": False,
        }

    async def call(self, input: Dict[str, Any], context: Any) -> ToolResult:
        """执行脚本调用。

        Args:
            input: 工具输入参数，必须符合 input_schema 定义。
            context: 执行上下文（当前未使用，保留接口一致性）。

        Returns:
            ToolResult: 工具执行结果。
        """
        # 提取并校验 script_path
        script_path_raw = input.get("script_path")
        if not script_path_raw or not isinstance(script_path_raw, str):
            return ToolResult(content="script_path 为必填字段且必须是字符串", is_error=True)

        # 提取 args，默认为空列表
        args = input.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            return ToolResult(content="args 必须是字符串数组", is_error=True)

        # 构建并校验路径
        script_path = Path(script_path_raw)

        # 拒绝绝对路径
        if script_path.is_absolute():
            return ToolResult(content="script_path 必须是相对路径", is_error=True)

        # 解析 workspace_root 与目标脚本的绝对路径
        workspace = Path(self._workspace_root).resolve()
        full_path = (workspace / script_path).resolve()

        # 确保归一化后的路径仍位于 workspace_root 内（防止 ../ 越界）
        try:
            full_path.relative_to(workspace)
        except ValueError:
            return ToolResult(content="script_path 超出工作区范围", is_error=True)

        # 检查后缀是否为 .py
        if full_path.suffix != ".py":
            return ToolResult(content="仅支持执行 .py 脚本", is_error=True)

        # 检查脚本文件是否存在
        if not full_path.exists():
            return ToolResult(content="指定脚本不存在", is_error=True)

        # 检查虚拟环境解释器是否存在且可执行
        python_exe = workspace / ".venv" / "bin" / "python"
        if not python_exe.exists():
            return ToolResult(
                content="虚拟环境解释器不存在: .venv/bin/python",
                is_error=True,
            )
        if not os.access(python_exe, os.X_OK):
            return ToolResult(
                content="虚拟环境解释器不可执行: .venv/bin/python",
                is_error=True,
            )

        # 使用 asyncio 创建子进程执行脚本（禁止 shell）
        try:
            proc = await asyncio.create_subprocess_exec(
                str(python_exe),
                str(full_path),
                *args,
                cwd=str(workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_data, stderr_data = await asyncio.wait_for(
                proc.communicate(),
                timeout=DEFAULT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            # 超时后先尝试优雅终止，若失败则强制杀死
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
            return ToolResult(
                content=f"脚本执行超时（{DEFAULT_TIMEOUT_SECONDS}秒）",
                is_error=True,
            )
        except Exception as e:
            return ToolResult(
                content=f"启动子进程失败: {str(e)}",
                is_error=True,
            )

        # 解码输出
        stdout_text = stdout_data.decode("utf-8", errors="replace")
        stderr_text = stderr_data.decode("utf-8", errors="replace")
        exit_code = proc.returncode

        # 非零退出码视为错误
        if exit_code != 0:
            err = stderr_text if stderr_text else stdout_text
            content = f"脚本退出码非零: {exit_code}"
            if err:
                content = f"{content}\n{err}"
            return ToolResult(content=content.strip(), is_error=True)

        # 成功返回 stdout 原文
        return ToolResult(content=stdout_text, is_error=False)
