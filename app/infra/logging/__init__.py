"""日志基础设施模块。

提供统一的日志配置和管理功能，支持 Web API 和 CLI 多入口共享配置。

典型用法:
    # 在应用入口初始化（如 start.py 或 cli.py）
    from app.infra.logging import setup_logging
    from app.config import Settings

    settings = Settings()
    setup_logging(settings)

    # 在业务模块中直接使用标准库 logging 获取 logger
    import logging

    logger = logging.getLogger(__name__)
    logger.info("业务操作成功")
    logger.error("发生错误: %s", error_message)

说明:
    - `setup_logging` / `shutdown_logging` 属于基础设施初始化边界。
    - `get_logger` 仍保留给 infra 内部与兼容场景使用，
      但 core / services / interfaces 不应再通过本模块获取 logger。
"""

from app.infra.logging.logger_manager import get_logger, setup_logging, shutdown_logging

__all__ = ["setup_logging", "shutdown_logging", "get_logger"]
