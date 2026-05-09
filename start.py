"""应用启动脚本。

封装 Uvicorn 服务器配置与命令行参数覆盖，支持开发/生产环境自动切换。
"""

from __future__ import annotations

import argparse
import logging
import sys


def parse_args() -> argparse.Namespace:
    """解析命令行参数。

    所有参数默认值为 None，实际值从 Settings 读取。
    命令行参数仅用于覆盖 Settings 中的配置。

    Returns:
        解析后的命名空间对象，包含所有参数值
    """
    parser = argparse.ArgumentParser(
        description="启动 Agent Framework API 服务",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--env",
        choices=["dev", "prod"],
        default=None,
        help="运行环境：dev（开发）或 prod（生产），默认从 APP_ENV 环境变量读取",
    )

    parser.add_argument(
        "--host",
        default=None,
        help="监听主机地址，默认从 APP_HOST 配置读取",
    )

    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="监听端口，默认从 APP_PORT 配置读取",
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="工作进程数，生产环境使用，默认从 UVICORN_WORKERS 或 CPU 核心数读取",
    )

    reload_group = parser.add_mutually_exclusive_group()
    reload_group.add_argument(
        "--reload",
        action="store_true",
        default=None,
        dest="reload",
        help="强制启用热加载",
    )
    reload_group.add_argument(
        "--no-reload",
        action="store_false",
        default=None,
        dest="reload",
        help="强制禁用热加载",
    )

    parser.add_argument(
        "--log-level",
        choices=["debug", "info", "error"],
        default=None,
        help="日志级别，默认从 LOG_LEVEL 环境变量读取",
    )

    return parser.parse_args()


def apply_overrides(settings, args: argparse.Namespace) -> tuple[bool | None, int | None]:
    """将命令行参数覆盖到 Settings 对象。

    Args:
        settings: Settings 配置对象
        args: 命令行参数命名空间

    Returns:
        (reload_override, workers_override) 元组，None 表示未覆盖
    """
    if args.env is not None:
        settings.app_env = args.env
    if args.host is not None:
        settings.app_host = args.host
    if args.port is not None:
        settings.app_port = args.port
    if args.log_level is not None:
        settings.log_level = args.log_level

    return args.reload, args.workers


def main() -> int:
    """主入口函数。

    Returns:
        退出码，0 表示成功
    """
    # 启动脚本复用 bootstrap 层提供的统一配置加载入口，
    # 避免这里再维护一套独立的 `.env` 与 Settings 初始化逻辑。
    from app.bootstrap.factory import get_project_root, initialize_runtime, load_settings

    # 启动脚本显式以仓库根目录为配置基准，
    # 避免 systemd 或其他守护进程从任意工作目录启动时解析到错误路径。
    project_root = get_project_root()
    settings = load_settings(project_root=project_root)

    args = parse_args()
    reload_override, workers_override = apply_overrides(settings, args)

    # 启动脚本同样复用 bootstrap 层的运行时初始化 helper。
    # 这样日志初始化策略只定义一处；即使 uvicorn worker 再次调用 bootstrap_app，
    # 也依赖日志模块内部的幂等保护，不会把初始化细节分散到多处。
    initialize_runtime(settings)

    logger = logging.getLogger(__name__)
    logger.info("日志系统初始化完成")

    try:
        try:
            import uvicorn
        except ImportError:
            logger.error("未找到 uvicorn，请安装依赖：pip install uvicorn")
            print("错误：未找到 uvicorn，请安装依赖：pip install uvicorn")
            return 1

        config = settings.get_uvicorn_config(reload=reload_override)

        if not settings.is_dev and workers_override is not None:
            config["workers"] = workers_override
            logger.info("生产环境工作进程数覆盖为: %d", workers_override)

        env_name = "开发环境" if settings.is_dev else "生产环境"
        print("=" * 50)
        print("启动 Agent Framework API 服务")
        print("=" * 50)
        print(f"环境: {env_name} ({settings.app_env})")
        print(f"地址: http://{settings.app_host}:{settings.app_port}")
        print(f"工作进程: {config['workers']}")
        print(f"热加载: {'开启' if config['reload'] else '关闭'}")
        print(f"日志级别: {settings.log_level}")
        print(f"访问日志: {'开启' if config['access_log'] else '关闭'}")
        print(f"项目根目录: {project_root}")
        print(f".env 路径: {project_root / '.env'}")
        print(f"日志目录: {settings.log_dir}")
        print(f"工作区根目录: {settings.workspace_root}")
        print(f"MCP 配置: {settings.mcp_servers_config_path}")
        print("=" * 50)

        logger.info(
            "启动服务: 环境=%s, 地址=%s:%d, 工作进程=%d, 热加载=%s, 日志级别=%s",
            settings.app_env,
            settings.app_host,
            settings.app_port,
            config["workers"],
            config["reload"],
            settings.log_level,
        )
        logger.info(
            "关键路径: project_root=%s, log_dir=%s, workspace_root=%s, mcp_config=%s",
            project_root,
            settings.log_dir,
            settings.workspace_root,
            settings.mcp_servers_config_path,
        )

        uvicorn.run(**config)
        return 0
    finally:
        # 无论是正常退出、导入失败还是启动异常，都要显式关闭日志后台线程与句柄。
        from app.infra.logging import shutdown_logging

        shutdown_logging()


if __name__ == "__main__":
    sys.exit(main())
