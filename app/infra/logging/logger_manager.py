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
    """支持按天轮转 + 按大小轮转的混合日志处理器。

    继承 TimedRotatingFileHandler 的按天轮转逻辑，并额外支持：
    当单文件超过 max_bytes 时，即使未到午夜也会触发轮转。
    轮转后的文件名格式为 {base}_{YYYYMMDD}_{序号}.log（同一天多次大小轮转时追加序号）。
    """

    def __init__(
        self,
        filename: str,
        max_bytes: int = 0,
        backup_count: int = 30,
        encoding: str | None = None,
        **kwargs: object,
    ) -> None:
        """初始化混合轮转处理器。

        Args:
            filename: 当前日志文件路径
            max_bytes: 单文件最大字节数，0 表示不限制大小
            backup_count: 保留的轮转文件数量
            encoding: 文件编码
        """
        super().__init__(
            filename,
            backupCount=backup_count,
            encoding=encoding,
            **kwargs,
        )
        # 单文件最大字节数
        self.max_bytes = max_bytes

    def shouldRollover(self, record: logging.LogRecord) -> int:
        """判断是否需要轮转。

        同时检查时间轮转和大小轮转两个条件，
        任一条件满足即触发轮转。

        Args:
            record: 当前日志记录

        Returns:
            非 0 表示需要轮转
        """
        # 先检查时间轮转（父类逻辑）
        if super().shouldRollover(record):
            return 1
        # 再检查大小轮转
        if self.max_bytes > 0:
            try:
                if os.path.getsize(self.baseFilename) >= self.max_bytes:
                    return 1
            except OSError:
                pass
        return 0


class PriorityDropQueue(queue.Queue):
    """支持优先级保底的有界日志队列。

    设计目标：
    1. 正常情况下保持完全非阻塞，避免请求线程因为日志写入卡住事件循环。
    2. 队列满时，DEBUG / INFO 直接丢弃。
    3. 队列满时，WARNING / ERROR 尝试挤掉队列里已有的低优先级日志。
    4. 不做 stderr 降级，不做额外统计，只保证高优先级日志尽量留住。
    """

    def put(self, item: logging.LogRecord | None, block: bool = True, timeout: float | None = None) -> None:
        """兼容标准 `Queue` 接口，但非阻塞场景走自定义优先级策略。

        当前生产代码和 `QueueListener.stop()` 都只会走 `put_nowait()`，
        因此这里仅在 `block=False` 时切到自定义逻辑，其余情况复用父类语义。

        Args:
            item: 要写入队列的日志记录；`None` 作为 `QueueListener` 的停止哨兵
            block: 是否阻塞等待
            timeout: 阻塞等待超时时间
        """
        if block:
            super().put(item, block=block, timeout=timeout)
            return

        self.put_nowait(item)

    def put_nowait(self, item: logging.LogRecord | None) -> None:
        """以非阻塞方式写入日志队列，并在满队列时按优先级丢弃。

        Args:
            item: 要写入的日志记录；`None` 表示停止哨兵

        Raises:
            queue.Full: 当低优先级日志在满队列下被拒绝时抛出
        """
        # 无界队列无需走优先级丢弃逻辑，直接复用父类实现。
        if self.maxsize <= 0:
            super().put_nowait(item)
            return

        with self.not_full:
            # 队列未满时直接入队，保持最短路径。
            if self._qsize() < self.maxsize:
                self._put(item)
                self.unfinished_tasks += 1
                self.not_empty.notify()
                return

            # 停止哨兵必须保证能塞进去，否则 listener 无法退出。
            # 这里直接丢掉最旧的一条待写日志，为关闭动作让路。
            if item is None:
                self._drop_oldest_unlocked()
                self._put(item)
                self.unfinished_tasks += 1
                self.not_empty.notify()
                return

            # 低优先级日志在满队列下直接拒绝，避免继续膨胀内存。
            if item.levelno < logging.WARNING:
                raise queue.Full

            # 高优先级日志尝试挤掉队列里已有的第一条低优先级日志。
            low_priority_index = self._find_first_low_priority_index_unlocked()
            if low_priority_index is None:
                raise queue.Full

            self._drop_index_unlocked(low_priority_index)
            self._put(item)
            self.unfinished_tasks += 1
            self.not_empty.notify()

    def _find_first_low_priority_index_unlocked(self) -> int | None:
        """在已持锁前提下查找第一条低优先级日志的下标。"""
        for index, queued_item in enumerate(self.queue):
            if queued_item is None:
                continue
            if queued_item.levelno < logging.WARNING:
                return index
        return None

    def _drop_oldest_unlocked(self) -> None:
        """在已持锁前提下移除最旧日志，为关闭哨兵腾出位置。"""
        if not self.queue:
            return

        self.queue.popleft()
        if self.unfinished_tasks > 0:
            self.unfinished_tasks -= 1

    def _drop_index_unlocked(self, index: int) -> None:
        """在已持锁前提下移除指定位置的日志，并同步修正任务计数。"""
        del self.queue[index]
        if self.unfinished_tasks > 0:
            self.unfinished_tasks -= 1


class NonBlockingQueueHandler(QueueHandler):
    """永不阻塞请求线程的队列日志处理器。

    `logging.handlers.QueueHandler` 在 `queue.Full` 时会走 `handleError()`，
    调试模式下可能把异常刷到 stderr。这里显式吞掉 `queue.Full`，
    保证“队列满时直接丢弃”的策略成立。
    """

    def emit(self, record: logging.LogRecord) -> None:
        """把日志记录放入队列；队列满时静默丢弃。"""
        try:
            prepared_record = self.prepare(record)
            self.enqueue(prepared_record)
        except queue.Full:
            return
        except Exception:
            self.handleError(record)

    def enqueue(self, record: logging.LogRecord) -> None:
        """强制使用非阻塞入队，避免请求线程等待后台写盘。"""
        self.queue.put_nowait(record)


class LoggerManager:
    """日志管理器，支持 Web API 和 CLI 多入口。

    使用单例模式确保全局配置一致性，支持幂等初始化（多次调用只生效一次）。

    Attributes:
        _initialized: 标记日志系统是否已初始化
        _settings: 当前使用的配置对象
    """

    _instance: LoggerManager | None = None
    _initialized: bool = False
    _settings: Settings | None = None
    _log_queue: PriorityDropQueue | None = None
    _queue_listener: QueueListener | None = None
    _queue_handler: NonBlockingQueueHandler | None = None
    _sink_handlers: list[logging.Handler]

    def __new__(cls) -> LoggerManager:
        """实现单例模式，确保全局只有一个管理器实例。"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            # 这些状态只在单例第一次创建时初始化一次。
            # 后续 setup / shutdown 会在这个实例上反复切换。
            cls._instance._initialized = False
            cls._instance._settings = None
            cls._instance._log_queue = None
            cls._instance._queue_listener = None
            cls._instance._queue_handler = None
            cls._instance._sink_handlers = []
        return cls._instance

    def setup(self, settings: Settings, console_output: bool | None = None) -> None:
        """初始化日志系统，可被多次调用（幂等）。

        根据配置设置日志级别、输出目标（控制台/文件）和格式。
        开发环境同时输出到控制台和文件，生产环境主要输出到文件。

        Args:
            settings: 应用配置对象，包含日志相关配置
            console_output: 兼容旧接口保留；当前统一按环境决定是否保留控制台 sink
        """
        # 如果已初始化且配置相同，直接返回（幂等）
        if self._initialized and self._settings is settings:
            return

        # 如果已有旧 listener 在跑，先完整关闭，避免线程和句柄泄漏。
        if self._initialized:
            self.shutdown()

        self._settings = settings

        # 确定日志级别
        level_map = {
            "debug": logging.DEBUG,
            "info": logging.INFO,
            "error": logging.ERROR,
        }
        log_level = level_map.get(settings.log_level.lower(), logging.INFO)

        # `console_output` 形参保留仅用于兼容旧调用方。
        # 当前产品策略已经固定：开发环境写 stdout + 文件，生产环境只写文件。
        _ = console_output
        enable_console_output = self._should_enable_console_output(settings)

        # 获取根日志器并清空现有处理器
        root_logger = logging.getLogger()
        root_logger.setLevel(log_level)

        # 清除现有处理器，避免重复添加
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)

        # 定义统一的日志格式
        # 格式：时间 | 级别 | 模块名:行号 | 消息
        log_format = logging.Formatter(
            "%(asctime)s | %(levelname)s | %(name)s:%(lineno)d | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        # 先构建真实 sink；根日志器后续只会挂一个队列处理器。
        sink_handlers: list[logging.Handler] = []

        # 开发环境保留 stdout sink，便于本地调试即时观察。
        if enable_console_output:
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setLevel(log_level)
            console_handler.setFormatter(log_format)
            sink_handlers.append(console_handler)

        # 文件 sink 在开发 / 生产环境都保留，Docker 场景也统一走文件日志。
        file_handler = self._create_file_handler(settings, log_format)
        if file_handler:
            # 文件日志遵循用户配置的 LOG_LEVEL
            file_handler.setLevel(log_level)
            file_handler.setFormatter(log_format)
            sink_handlers.append(file_handler)

        # 组装非阻塞日志队列。
        # 真正的磁盘 / stdout I/O 全部转移到后台 listener 线程执行。
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

        # 记录日志系统初始化完成
        logger = logging.getLogger(__name__)
        logger.info("日志系统初始化完成，级别: %s", settings.log_level)

    def shutdown(self) -> None:
        """关闭日志系统，释放后台线程和所有 handler 资源。"""
        root_logger = logging.getLogger()

        # 先从根日志器移除队列 handler，阻止新的日志继续进入待关闭队列。
        if self._queue_handler is not None and self._queue_handler in root_logger.handlers:
            root_logger.removeHandler(self._queue_handler)

        # 兜底移除根日志器上的残留 handler，避免多次 setup 后重复挂载。
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)

        # 停掉后台 listener 线程；自定义队列会确保停止哨兵能入队。
        if self._queue_listener is not None:
            self._queue_listener.stop()

        # 关闭队列 handler，释放内部资源引用。
        if self._queue_handler is not None:
            self._queue_handler.close()

        # 关闭真实 sink，释放 stdout / 文件句柄。
        for handler in self._sink_handlers:
            handler.close()

        self._initialized = False
        self._settings = None
        self._log_queue = None
        self._queue_listener = None
        self._queue_handler = None
        self._sink_handlers = []

    def _should_enable_console_output(self, settings: Settings) -> bool:
        """按环境决定是否保留 stdout sink。"""
        is_dev = getattr(settings, "is_dev", None)
        if isinstance(is_dev, bool):
            return is_dev
        return getattr(settings, "app_env", "dev") == "dev"

    def _create_file_handler(
        self,
        settings: Settings,
        formatter: logging.Formatter,
    ) -> SizeTimedRotatingFileHandler | None:
        """创建支持按天 + 按大小轮转的日志文件处理器。

        日志文件按天轮转，同时支持按大小轮转，保留最近 30 天的日志。
        当前日志文件名：{log_file_name}.log（如 app.log）
        轮转后文件名：{log_file_name}_YYYYMMDD.log（如 app_20260405.log）

        Args:
            settings: 应用配置对象
            formatter: 日志格式器

        Returns:
            配置好的 SizeTimedRotatingFileHandler，如果创建失败则返回 None
        """
        try:
            # 确定日志目录路径（相对于项目根目录）
            # 从 app/config.py 所在位置推断项目根目录
            project_root = Path(__file__).parent.parent.parent.parent
            log_dir = project_root / settings.log_dir

            # 确保日志目录存在
            log_dir.mkdir(parents=True, exist_ok=True)

            # 构建日志文件路径（不含日期后缀，由处理器自动添加）
            log_file = log_dir / f"{settings.log_file_name}.log"

            # 创建混合轮转处理器（按天 + 按大小）
            # when='midnight' 表示每天午夜轮转
            # max_bytes 来自配置，超过限制也会触发轮转
            # backupCount=30 表示保留 30 个轮转文件
            handler = SizeTimedRotatingFileHandler(
                filename=str(log_file),
                when="midnight",
                interval=1,
                # 这里必须使用包装类自己的 `backup_count` 参数名，
                # 避免和父类原生 `backupCount` 通过 `**kwargs` 重复透传。
                backup_count=30,
                max_bytes=settings.log_max_bytes,
                encoding="utf-8",
            )

            # 设置文件名后缀格式（仅日期部分，用于匹配和排序）
            handler.suffix = "%Y%m%d"

            # 自定义轮转文件命名：将默认的 app.log.20260405 改为 app_20260405.log
            # 同一天多次大小轮转时追加序号：app_20260405_1.log、app_20260405_2.log
            # 默认命名规则为 {filename}.{suffix}，即 app.log.20260405
            # 需要替换为 {log_file_name}_{suffix}.log，即 app_20260405.log

            def _namer(default_name: str) -> str:
                """自定义轮转日志文件名。

                将 TimedRotatingFileHandler 默认生成的
                'app.log.20260405' 转换为 'app_20260405.log'。
                如果该文件已存在（同一天多次大小轮转），
                追加递增序号 'app_20260405_1.log'、'app_20260405_2.log' 等。

                Args:
                    default_name: 默认轮转文件名，格式为 {base}.log.{date}

                Returns:
                    修正后的文件名，格式为 {base}_{date}.log 或 {base}_{date}_{n}.log
                """
                # 去掉目录路径，只取文件名
                dir_name = os.path.dirname(default_name)
                file_name = os.path.basename(default_name)
                # 将 'app.log.20260405' 拆分重组为 'app_20260405.log'
                parts = file_name.rsplit(".", 2)
                if len(parts) == 3:
                    # parts = ['app', 'log', '20260405']
                    new_name = f"{parts[0]}_{parts[2]}.log"
                else:
                    # 降级：无法解析时保留原名
                    new_name = file_name
                result = os.path.join(dir_name, new_name) if dir_name else new_name

                # 同一天多次轮转时，追加递增序号避免覆盖
                if os.path.exists(result):
                    seq = 1
                    while True:
                        # 生成带序号的文件名：app_20260405_1.log
                        seq_name = f"{parts[0]}_{parts[2]}_{seq}.log"
                        result = os.path.join(dir_name, seq_name) if dir_name else seq_name
                        if not os.path.exists(result):
                            break
                        seq += 1

                return result

            handler.namer = _namer

            return handler

        except (OSError, PermissionError) as e:
            # 如果创建文件处理器失败，记录到控制台
            console_handler = logging.StreamHandler(sys.stderr)
            console_handler.setLevel(logging.ERROR)
            console_handler.setFormatter(formatter)
            temp_logger = logging.getLogger(__name__)
            temp_logger.addHandler(console_handler)
            temp_logger.error("创建日志文件处理器失败: %s", e)
            return None

    def get_logger(self, name: str) -> logging.Logger:
        """获取命名日志器。

        如果日志系统尚未初始化，会触发一个警告日志。

        Args:
            name: 日志器名称，建议使用 __name__

        Returns:
            配置好的日志器实例
        """
        if not self._initialized:
            # 如果未初始化，返回一个默认日志器并输出警告
            logger = logging.getLogger(name)
            if not logger.handlers:
                # 添加一个临时的空处理器避免报错
                logger.addHandler(logging.NullHandler())
        else:
            logger = logging.getLogger(name)

        return logger


# 全局管理器实例
_manager = LoggerManager()


def setup_logging(settings: Settings, console_output: bool | None = None) -> None:
    """初始化日志系统，供各入口调用。

    这是一个便捷函数，封装了 LoggerManager.setup() 方法。
    可被多次调用，具有幂等性。

    Args:
        settings: 应用配置对象
        console_output: 兼容旧接口保留；当前实际输出策略由环境决定

    Example:
        >>> from app.config import Settings
        >>> from app.infra.logging import setup_logging
        >>> settings = Settings()
        >>> setup_logging(settings)
    """
    _manager.setup(settings, console_output)


def shutdown_logging() -> None:
    """关闭日志系统，供入口脚本在退出前显式清理资源。"""
    _manager.shutdown()


def get_logger(name: str) -> logging.Logger:
    """获取命名日志器。

    当前主要用于 infra 内部与兼容场景。
    core / services / interfaces 等上层模块应直接使用标准库：
        import logging
        logger = logging.getLogger(__name__)

    Args:
        name: 日志器名称，通常使用 __name__

    Returns:
        日志器实例

    Example:
        >>> from app.infra.logging import get_logger
        >>> logger = get_logger(__name__)
        >>> logger.info("用户登录成功")
    """
    return _manager.get_logger(name)
