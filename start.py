"""应用启动脚本。

统一封装 Uvicorn 服务器配置，支持开发/生产环境自动切换，
并确保生产环境在不同工作目录下也能稳定解析项目配置。

使用方法:
    python start.py [选项]

基本示例:
    # 开发环境启动（默认，启用热加载）
    python start.py

    # 生产环境启动（多进程模式）
    APP_ENV=prod python start.py --workers 4

    # 指定端口和主机
    python start.py --host 0.0.0.0 --port 8080

    # 禁用热加载
    python start.py --no-reload

命令行参数说明:
    --env {dev,prod}
        运行环境选择。
        - dev: 开发环境，启用热加载、API 文档，单进程
        - prod: 生产环境，禁用热加载，使用多进程
        默认值: 从 APP_ENV 环境变量读取，未设置则使用 dev

    --host HOST
        服务器监听的主机地址。
        - 127.0.0.1: 仅本地访问（安全，适合开发和内网）
        - 0.0.0.0: 监听所有网络接口（允许外部访问，生产环境使用）
        默认值: 从 APP_HOST 环境变量读取，未设置则使用 127.0.0.1

    --port PORT
        服务器监听的端口号。
        默认值: 从 APP_PORT 环境变量读取，未设置则使用 8000

    --workers WORKERS
        Uvicorn 工作进程数。
        - 开发环境: 强制为 1（热加载不支持多进程）
        - 生产环境: 默认等于 CPU 核心数，可根据负载调整
        建议: CPU 密集型设为 CPU 核心数，IO 密集型可设为 2-4 倍

    --reload / --no-reload
        启用或禁用热加载。代码文件变更时自动重启服务器。
        - 开发环境: 默认开启
        - 生产环境: 默认关闭
        注意: 热加载会消耗额外资源，生产环境禁用

    --log-level {debug,info,error}
        日志输出级别。
        - debug: 调试信息，最详细
        - info: 一般信息（推荐）
        - error: 错误及以上
        默认值: 从 LOG_LEVEL 环境变量读取，未设置则使用 info

环境变量优先级:
    1. 命令行参数（最高优先级）
    2. .env 文件中的配置
    3. 参数默认值（最低优先级）

生产环境部署建议:
    APP_ENV=prod .venv/bin/python start.py --workers 4 --host 0.0.0.0

Docker 部署示例:
    docker run -p 8000:8000 -e APP_ENV=prod myapp python start.py --workers 4
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

    # 运行环境覆盖
    parser.add_argument(
        "--env",
        choices=["dev", "prod"],
        default=None,
        help="运行环境：dev（开发）或 prod（生产），默认从 APP_ENV 环境变量读取",
    )

    # 主机地址覆盖
    parser.add_argument(
        "--host",
        default=None,
        help="监听主机地址，默认从 APP_HOST 配置读取",
    )

    # 端口覆盖
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="监听端口，默认从 APP_PORT 配置读取",
    )

    # 工作进程数覆盖
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="工作进程数，生产环境使用，默认从 UVICORN_WORKERS 或 CPU 核心数读取",
    )

    # 热加载开关（支持 --reload 和 --no-reload）
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

    # 日志级别覆盖
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
    # 应用基础配置覆盖
    if args.env is not None:
        settings.app_env = args.env
    if args.host is not None:
        settings.app_host = args.host
    if args.port is not None:
        settings.app_port = args.port
    if args.log_level is not None:
        settings.log_level = args.log_level

    # 返回可能需要特殊处理的覆盖值
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

    # 解析命令行参数
    args = parse_args()

    # 应用命令行覆盖
    reload_override, workers_override = apply_overrides(settings, args)

    # 启动脚本同样复用 bootstrap 层的运行时初始化 helper。
    # 这样日志初始化策略只定义一处；即使 uvicorn worker 再次调用 bootstrap_app，
    # 也依赖日志模块内部的幂等保护，不会把初始化细节分散到多处。
    initialize_runtime(settings)

    # 获取日志器。
    # 入口层只从 infra 获取初始化能力，具体 logger 仍统一走标准库。
    logger = logging.getLogger(__name__)
    logger.info("日志系统初始化完成")

    try:
        # 尝试导入 uvicorn
        try:
            import uvicorn
        except ImportError:
            logger.error("未找到 uvicorn，请安装依赖：pip install uvicorn")
            print("错误：未找到 uvicorn，请安装依赖：pip install uvicorn")
            return 1

        # 获取 Uvicorn 配置
        config = settings.get_uvicorn_config(reload=reload_override)

        # 生产环境下应用 workers 覆盖
        if not settings.is_dev and workers_override is not None:
            config["workers"] = workers_override
            logger.info("生产环境工作进程数覆盖为: %d", workers_override)

        # 打印启动信息
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

        # 记录启动日志
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

        # 启动服务器
        uvicorn.run(**config)
        return 0
    finally:
        # 无论是正常退出、导入失败还是启动异常，都要显式关闭日志后台线程与句柄。
        from app.infra.logging import shutdown_logging

        shutdown_logging()


if __name__ == "__main__":
    sys.exit(main())
