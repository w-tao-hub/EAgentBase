"""应用启动 bootstrap 工厂。

编排配置加载、初始化、容器构建，最后调用纯装配器创建 FastAPI 应用实例。
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI

from app.bootstrap.container import Container
from app.config import Settings
from app.infra.logging import setup_logging
from app.main import create_app


def get_project_root() -> Path:
    """返回仓库根目录。"""
    return Path(__file__).resolve().parents[2]


def _resolve_project_path(project_root: Path, raw_path: str) -> str:
    """把配置中的路径字段解析成稳定绝对路径。

    Args:
        project_root: 项目根目录绝对路径
        raw_path: 原始配置值，可能是相对路径也可能已是绝对路径

    Returns:
        解析后的绝对路径字符串
    """
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return str(candidate)
    return str((project_root / candidate).resolve())


def normalize_settings_paths(settings: Settings, project_root: Path | None = None) -> Settings:
    """统一把路径类配置锚定到项目根目录。

    Args:
        settings: 已完成环境变量解析的配置对象
        project_root: 可选项目根目录，未传时自动推断

    Returns:
        已完成路径归一化的同一个 Settings 对象
    """
    resolved_project_root = project_root or get_project_root()

    # 日志目录、工具工作区与 MCP 配置文件都属于部署期关键路径。
    # 统一在 bootstrap 阶段归一化后，后续各层就不再依赖调用时的 `cwd`。
    settings.log_dir = _resolve_project_path(resolved_project_root, settings.log_dir)
    settings.workspace_root = _resolve_project_path(resolved_project_root, settings.workspace_root)
    settings.mcp_servers_config_path = _resolve_project_path(
        resolved_project_root,
        settings.mcp_servers_config_path,
    )
    return settings


def load_settings(project_root: Path | None = None) -> Settings:
    """加载环境变量并构造应用配置。

    Args:
        project_root: 可选项目根目录，未传时自动推断

    Returns:
        已完成环境变量解析的 Settings 实例
    """
    resolved_project_root = project_root or get_project_root()
    dotenv_path = resolved_project_root / ".env"

    # 先把 `.env` 中的配置加载进当前进程环境变量，
    # 这样后续 Settings 与 LiteLLM 相关依赖都能读取到统一来源。
    load_dotenv(dotenv_path=dotenv_path)

    # 统一由 Settings 承接配置读取与默认值逻辑，
    # 避免入口层直接散落环境变量解析细节。
    settings = Settings(_env_file=dotenv_path)

    # 路径字段在这里统一锚定到项目根目录，
    # 避免 systemd、测试或外部脚本切换工作目录后出现相对路径漂移。
    return normalize_settings_paths(settings=settings, project_root=resolved_project_root)


def initialize_runtime(settings: Settings) -> None:
    """执行启动阶段的全局初始化。

    Args:
        settings: 已完成解析的应用配置
    """
    # 当前启动期唯一显式全局初始化动作是日志初始化。
    # 未来如果要加入 tracing / metrics，也应继续留在这一层，而不是回流到 create_app。
    setup_logging(settings)


def build_container(settings: Settings) -> Container:
    """构建依赖注入容器。

    Args:
        settings: 已完成解析的应用配置

    Returns:
        装配完成的依赖容器
    """
    # 容器仍是唯一组合根；
    # bootstrap 层只负责编排，不把依赖实例化细节展开到入口函数中。
    return Container.create(settings=settings)


def bootstrap_app() -> FastAPI:
    """公开无参服务启动入口。

    Returns:
        已完成 bootstrap 与装配的 FastAPI 应用实例
    """
    settings = load_settings()
    initialize_runtime(settings)
    container = build_container(settings)
    return create_app(settings=settings, container=container)
