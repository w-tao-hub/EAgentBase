"""日志管理器模块。

提供统一的日志配置和初始化功能，支持 Web API 和 CLI 多入口共享配置。
使用单例模式确保全局只有一个日志配置实例。
"""

from __future__ import annotations

import logging
import os
import queue
import sys
from logging.handlers import QueueHandler, QueueListener, TimedRotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import Settings


class SizeTimedRotatingFileHandler(TimedRotatingFileHandler):
    """按天 + 按大小混合轮转的日志处理器。"""

    def __init__(
        self,
        filename: str,
        max_bytes: int = 0,
        backup_count: int = 30,
        encoding: str | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(
            filename,
            backupCount=backup_count,
            encoding=encoding,
            **kwargs,
        )
        self.max_bytes = max_bytes

    def shouldRollover(self, record: logging.LogRecord) -> int:
        """判断是否需要轮转（时间或大小任一条件满足即触发）。"""
        if super().shouldRollover(record):
            return 1
        if self.max_bytes > 0:
            try:
                if os.path.getsize(self.baseFilename) >= self.max_bytes:
                    return 1
            except OSError:
                pass
        return 0


class PriorityDropQueue(queue.Queue):
    """有界日志队列，满队列时低优先级日志直接丢弃，高优先级挤掉低优先级。"""

    def put(self, item: logging.LogRecord | None, block: bool = True, timeout: float | None = None) -> None:
        if block:
            super().put(item, block=block, timeout=timeout)
            return
        self.put_nowait(item)

    def put_nowait(self, item: logging.LogRecord | None) -> None:
        if self.maxsize <= 0:
            super().put_nowait(item)
            return

        with self.not_full:
            if self._qsize() < self.maxsize:
                self._put(item)
                self.unfinished_tasks += 1
                self.not_empty.notify()
                return

            # 停止哨兵直接丢弃最旧日志
            if item is None:
                self._drop_oldest_unlocked()
                self._put(item)
                self.unfinished_tasks += 1
                self.not_empty.notify()
                return

            if item.levelno < logging.WARNING:
                raise queue.Full

            low_priority_index = self._find_first_low_priority_index_unlocked()
            if low_priority_index is None:
                raise queue.Full

            self._drop_index_unlocked(low_priority_index)
            self._put(item)
            self.unfinished_tasks += 1
            self.not_empty.notify()

    def _find_first_low_priority_index_unlocked(self) -> int | None:
        """查找队列中第一条低优先级日志（已持锁）。"""
        for index, queued_item in enumerate(self.queue):
            if queued_item is None:
                continue
            if queued_item.levelno < logging.WARNING:
                return index
        return None

    def _drop_oldest_unlocked(self) -> None:
        """移除最旧日志（已持锁）。"""
        if not self.queue:
            return
        self.queue.popleft()
        if self.unfinished_tasks > 0:
            self.unfinished_tasks -= 1

    def _drop_index_unlocked(self, index: int) -> None:
        """移除指定位置日志（已持锁）。"""
        del self.queue[index]
        if self.unfinished_tasks > 0:
            self.unfinished_tasks -= 1


class NonBlockingQueueHandler(QueueHandler):
    """永不阻塞请求线程的队列日志处理器，满时静默丢弃。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            prepared_record = self.prepare(record)
            self.enqueue(prepared_record)
        except queue.Full:
            return
        except Exception:
            self.handleError(record)

    def enqueue(self, record: logging.LogRecord) -> None:
        self.queue.put_nowait(record)


class LoggerManager:
    """日志管理器（单例，支持幂等初始化）。"""

    _instance: LoggerManager | None = None
    _initialized: bool = False
    _settings: Settings | None = None
    _log_queue: PriorityDropQueue | None = None
    _queue_listener: QueueListener | None = None
    _queue_handler: NonBlockingQueueHandler | None = None
    _sink_handlers: list[logging.Handler]

    def __new__(cls) -> LoggerManager:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
            cls._instance._settings = None
            cls._instance._log_queue = None
            cls._instance._queue_listener = None
            cls._instance._queue_handler = None
            cls._instance._sink_handlers = []
        return cls._instance

    def setup(self, settings: Settings, console_output: bool | None = None) -> None:
        """初始化日志系统（幂等，可多次调用）。"""
        if self._initialized and self._settings is settings:
            return
        if self._initialized:
            self.shutdown()

        self._settings = settings

        level_map = {
            "debug": logging.DEBUG,
            "info": logging.INFO,
            "error": logging.ERROR,
        }
        log_level = level_map.get(settings.log_level.lower(), logging.INFO)

        _ = console_output
        enable_console_output = self._should_enable_console_output(settings)

        root_logger = logging.getLogger()
        root_logger.setLevel(log_level)

        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)

        log_format = logging.Formatter(
            "%(asctime)s | %(levelname)s | %(name)s:%(lineno)d | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        sink_handlers: list[logging.Handler] = []

        if enable_console_output:
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setLevel(log_level)
            console_handler.setFormatter(log_format)
            sink_handlers.append(console_handler)

        file_handler = self._create_file_handler(settings, log_format)
        if file_handler:
            file_handler.setLevel(log_level)
            file_handler.setFormatter(log_format)
            sink_handlers.append(file_handler)

        self._log_queue = PriorityDropQueue(maxsize=settings.log_queue_maxsize)
        self._queue_handler = NonBlockingQueueHandler(self._log_queue)
        self._queue_listener = QueueListener(
            self._log_queue,
            *sink_handlers,
            respect_handler_level=True,
        )
        self._queue_listener.start()
        root_logger.addHandler(self._queue_handler)
        self._sink_handlers = sink_handlers

        self._initialized = True

        logger = logging.getLogger(__name__)
        logger.info("日志系统初始化完成，级别: %s", settings.log_level)

    def shutdown(self) -> None:
        """关闭日志系统，释放后台线程和 handler 资源。"""
        root_logger = logging.getLogger()

        if self._queue_handler is not None and self._queue_handler in root_logger.handlers:
            root_logger.removeHandler(self._queue_handler)

        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)

        if self._queue_listener is not None:
            self._queue_listener.stop()

        if self._queue_handler is not None:
            self._queue_handler.close()

        for handler in self._sink_handlers:
            handler.close()

        self._initialized = False
        self._settings = None
        self._log_queue = None
        self._queue_listener = None
        self._queue_handler = None
        self._sink_handlers = []

    def _should_enable_console_output(self, settings: Settings) -> bool:
        is_dev = getattr(settings, "is_dev", None)
        if isinstance(is_dev, bool):
            return is_dev
        return getattr(settings, "app_env", "dev") == "dev"

    def _create_file_handler(
        self,
        settings: Settings,
        formatter: logging.Formatter,
    ) -> SizeTimedRotatingFileHandler | None:
        """创建按天 + 按大小轮转的日志文件处理器。"""
        try:
            project_root = Path(__file__).parent.parent.parent.parent
            log_dir = project_root / settings.log_dir
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / f"{settings.log_file_name}.log"

            handler = SizeTimedRotatingFileHandler(
                filename=str(log_file),
                when="midnight",
                interval=1,
                backup_count=30,
                max_bytes=settings.log_max_bytes,
                encoding="utf-8",
            )

            handler.suffix = "%Y%m%d"

            def _namer(default_name: str) -> str:
                """将默认 app.log.20260405 改为 app_20260405.log。"""
                dir_name = os.path.dirname(default_name)
                file_name = os.path.basename(default_name)
                parts = file_name.rsplit(".", 2)
                if len(parts) == 3:
                    new_name = f"{parts[0]}_{parts[2]}.log"
                else:
                    new_name = file_name
                result = os.path.join(dir_name, new_name) if dir_name else new_name

                if os.path.exists(result):
                    seq = 1
                    while True:
                        seq_name = f"{parts[0]}_{parts[2]}_{seq}.log"
                        result = os.path.join(dir_name, seq_name) if dir_name else seq_name
                        if not os.path.exists(result):
                            break
                        seq += 1
                return result

            handler.namer = _namer
            return handler

        except (OSError, PermissionError) as e:
            console_handler = logging.StreamHandler(sys.stderr)
            console_handler.setLevel(logging.ERROR)
            console_handler.setFormatter(formatter)
            temp_logger = logging.getLogger(__name__)
            temp_logger.addHandler(console_handler)
            temp_logger.error("创建日志文件处理器失败: %s", e)
            return None

    def get_logger(self, name: str) -> logging.Logger:
        """获取命名日志器。"""
        if not self._initialized:
            logger = logging.getLogger(name)
            if not logger.handlers:
                logger.addHandler(logging.NullHandler())
        else:
            logger = logging.getLogger(name)
        return logger


# 全局管理器实例
_manager = LoggerManager()


def setup_logging(settings: Settings, console_output: bool | None = None) -> None:
    """初始化日志系统（便捷函数，封装 LoggerManager.setup）。"""
    _manager.setup(settings, console_output)


def shutdown_logging() -> None:
    """关闭日志系统。"""
    _manager.shutdown()


def get_logger(name: str) -> logging.Logger:
    """获取命名日志器。"""
    return _manager.get_logger(name)
