"""RunPythonScriptTool 单元测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from app.core.models.execution_context import ExecutionContext
from app.core.models.tool import ToolResult
from app.infra.tools.run_python_script_tool import (
    RunPythonScriptTool,  # 导入被测工具
    DEFAULT_TIMEOUT_SECONDS,  # 导入默认超时常量
)


def _build_ctx() -> ExecutionContext:
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
        session_id="s1",
        metadata={},
        agent=agent,
    )


def _make_workspace(tmp_path: Path) -> Path:
    """构造带有假 .venv/bin/python 的临时工作区。"""
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").symlink_to(sys.executable)
    return tmp_path


def _write_script(workspace: Path, rel_path: str, content: str) -> Path:
    """在工作区中写入测试脚本。"""
    script = workspace / rel_path
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(content, encoding="utf-8")
    return script


class TestInputValidation:
    """测试输入参数校验。"""

    async def test_missing_script_path_returns_error(self, tmp_path: Path):
        """缺失 script_path 应返回错误。"""
        workspace = _make_workspace(tmp_path)
        tool = RunPythonScriptTool(workspace_root=str(workspace))
        result = await tool.call({"args": []}, _build_ctx())
        assert isinstance(result, ToolResult)
        assert result.is_error is True
        assert "script_path" in result.content

    async def test_absolute_path_rejected(self, tmp_path: Path):
        """绝对路径应被拒绝。"""
        workspace = _make_workspace(tmp_path)
        tool = RunPythonScriptTool(workspace_root=str(workspace))
        result = await tool.call({"script_path": "/etc/passwd"}, _build_ctx())
        assert result.is_error is True
        assert "相对路径" in result.content

    async def test_path_traversal_rejected(self, tmp_path: Path):
        """../ 越界路径应被拒绝。"""
        workspace = _make_workspace(tmp_path)
        tool = RunPythonScriptTool(workspace_root=str(workspace))
        result = await tool.call({"script_path": "../outside.py"}, _build_ctx())
        assert result.is_error is True
        assert "超出工作区范围" in result.content

    async def test_non_py_file_rejected(self, tmp_path: Path):
        """非 .py 文件应被拒绝。"""
        workspace = _make_workspace(tmp_path)
        _write_script(workspace, "script.sh", "echo hello")
        tool = RunPythonScriptTool(workspace_root=str(workspace))
        result = await tool.call({"script_path": "script.sh"}, _build_ctx())
        assert result.is_error is True
        assert ".py" in result.content

    async def test_missing_script_returns_error(self, tmp_path: Path):
        """脚本不存在应返回错误。"""
        workspace = _make_workspace(tmp_path)
        tool = RunPythonScriptTool(workspace_root=str(workspace))
        result = await tool.call({"script_path": "not_exist.py"}, _build_ctx())
        assert result.is_error is True
        assert "不存在" in result.content

    async def test_missing_python_interpreter_returns_error(self, tmp_path: Path):
        """.venv/bin/python 不存在应返回错误。"""
        # 只创建脚本文件，但不创建 .venv，使校验流到解释器检查
        _write_script(tmp_path, "foo.py", "print('ok')")
        tool = RunPythonScriptTool(workspace_root=str(tmp_path))
        result = await tool.call({"script_path": "foo.py"}, _build_ctx())
        assert result.is_error is True
        assert "解释器不存在" in result.content


class TestExecution:
    """测试正常与异常执行语义。"""

    async def test_success_run_stdout(self, tmp_path: Path):
        """成功执行脚本应原样返回 stdout。"""
        workspace = _make_workspace(tmp_path)
        _write_script(workspace, "hello.py", "print('hello from tool')")
        tool = RunPythonScriptTool(workspace_root=str(workspace))
        result = await tool.call({"script_path": "hello.py"}, _build_ctx())
        assert result.is_error is False
        assert "hello from tool" in result.content

    async def test_args_passed_correctly(self, tmp_path: Path):
        """args 数组应正确透传给脚本 argv。"""
        workspace = _make_workspace(tmp_path)
        _write_script(
            workspace,
            "args.py",
            "import sys, json; print(json.dumps(sys.argv[1:]))",
        )
        tool = RunPythonScriptTool(workspace_root=str(workspace))
        result = await tool.call(
            {"script_path": "args.py", "args": ["a", "b", "c"]},
            _build_ctx(),
        )
        assert result.is_error is False
        assert json.loads(result.content) == ["a", "b", "c"]

    async def test_nonzero_exit_returns_error_with_code(self, tmp_path: Path):
        """脚本非零退出码应返回错误并携带 exit code。"""
        workspace = _make_workspace(tmp_path)
        _write_script(
            workspace,
            "fail.py",
            "import sys; print('stdout'); print('stderr', file=sys.stderr); sys.exit(42)",
        )
        tool = RunPythonScriptTool(workspace_root=str(workspace))
        result = await tool.call({"script_path": "fail.py"}, _build_ctx())
        assert result.is_error is True
        assert "42" in result.content
        # 优先展示 stderr
        assert "stderr" in result.content

    async def test_nonzero_exit_fallback_to_stdout_when_stderr_empty(self, tmp_path: Path):
        """非零退出码且 stderr 为空时应退回 stdout。"""
        workspace = _make_workspace(tmp_path)
        _write_script(workspace, "fail_no_stderr.py", "print('only stdout'); exit(1)")
        tool = RunPythonScriptTool(workspace_root=str(workspace))
        result = await tool.call({"script_path": "fail_no_stderr.py"}, _build_ctx())
        assert result.is_error is True
        assert "only stdout" in result.content

    async def test_timeout_returns_error(self, monkeypatch, tmp_path: Path):
        """脚本执行超时应返回错误并结束进程。"""
        # 将超时常量临时调低，避免测试等待 60 秒
        monkeypatch.setattr(
            "app.infra.tools.run_python_script_tool.DEFAULT_TIMEOUT_SECONDS",
            0.1,
        )
        workspace = _make_workspace(tmp_path)
        _write_script(workspace, "sleep.py", "import time; time.sleep(10)")
        tool = RunPythonScriptTool(workspace_root=str(workspace))
        result = await tool.call({"script_path": "sleep.py"}, _build_ctx())
        assert result.is_error is True
        assert "超时" in result.content

    async def test_invalid_args_type_returns_error(self, tmp_path: Path):
        """args 类型非法应返回错误。"""
        workspace = _make_workspace(tmp_path)
        tool = RunPythonScriptTool(workspace_root=str(workspace))
        result = await tool.call(
            {"script_path": "foo.py", "args": "not_a_list"},
            _build_ctx(),
        )
        assert result.is_error is True
        assert "args" in result.content


class TestToolRegistrySmoke:
    """测试工具注册后的基础属性。"""

    def test_name_and_schema(self, tmp_path: Path):
        """验证工具名称与 schema 结构。"""
        workspace = _make_workspace(tmp_path)
        tool = RunPythonScriptTool(workspace_root=str(workspace))
        assert tool.name == "run_python_script"
        assert tool.input_schema["type"] == "object"
        assert "script_path" in tool.input_schema["properties"]
        assert "args" in tool.input_schema["properties"]
