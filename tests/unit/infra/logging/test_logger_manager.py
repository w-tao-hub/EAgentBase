"""日志管理器回归测试。"""

from __future__ import annotations

import logging
import queue
from types import SimpleNamespace

import pytest

from app.infra.logging.logger_manager import (
    LoggerManager,
    NonBlockingQueueHandler,
    PriorityDropQueue,
    SizeTimedRotatingFileHandler,
)


def test_create_file_handler_does_not_duplicate_backup_count(
    tmp_path,
) -> None:
    """验证创建文件处理器时不会因为 `backupCount` 重复传参而抛错。"""
    # 构造日志管理器实例，复用生产代码中的处理器创建逻辑。
    manager = LoggerManager()

    # 这里只提供 `_create_file_handler` 真正依赖的最小配置字段，
    # 用来锁定当前回归问题，不把测试范围扩散到 `Settings` 解析逻辑。
    settings = SimpleNamespace(
        log_dir=str(tmp_path),
        log_file_name="app",
        log_max_bytes=1024,
    )

    # 构造一个最小格式器，满足 `_create_file_handler` 的入参要求。
    formatter = logging.Formatter("%(message)s")

    # 该调用在回归前会因为 `backupCount` 被重复传入父类构造函数而直接抛出 `TypeError`。
    handler = manager._create_file_handler(settings, formatter)

    # 修复后必须成功返回自定义混合轮转处理器，而不是在初始化阶段崩溃。
    assert isinstance(handler, SizeTimedRotatingFileHandler)

    # 同时确认保留数量仍然保持既定默认值，避免修复时把轮转语义改坏。
    assert handler.backupCount == 30

    # 显式关闭文件句柄，避免测试结束后产生平台相关的资源占用问题。
    handler.close()


def test_priority_drop_queue_replaces_low_priority_record_for_warning() -> None:
    """验证高优先级日志在队列已满时可挤掉低优先级日志。"""
    # 创建一个容量为 2 的测试队列，模拟高并发下队列被占满的场景。
    log_queue = PriorityDropQueue(maxsize=2)

    # 先放入两条低优先级日志，把队列占满。
    log_queue.put_nowait(logging.makeLogRecord({"levelno": logging.INFO, "msg": "info-1"}))
    log_queue.put_nowait(logging.makeLogRecord({"levelno": logging.DEBUG, "msg": "debug-1"}))

    # 再放入一条 warning，应当优先保底进入队列，而不是直接抛出 Full。
    warning_record = logging.makeLogRecord({"levelno": logging.WARNING, "msg": "warning-1"})
    log_queue.put_nowait(warning_record)

    # 读取当前队列内容，验证 warning 已进入，且至少有一条低优先级日志被挤掉。
    records = [log_queue.get_nowait(), log_queue.get_nowait()]
    record_messages = [record.msg for record in records]
    assert "warning-1" in record_messages
    assert len(record_messages) == 2


def test_priority_drop_queue_drops_low_priority_record_when_full() -> None:
    """验证队列已满时新的低优先级日志会被直接拒绝。"""
    # 创建一个容量为 1 的测试队列，方便稳定复现满队列场景。
    log_queue = PriorityDropQueue(maxsize=1)

    # 先放入一条 info 日志，占满队列。
    log_queue.put_nowait(logging.makeLogRecord({"levelno": logging.INFO, "msg": "info-1"}))

    # 再放入另一条 info，应该直接抛出 Full，表示该低优先级日志会被丢弃。
    with pytest.raises(queue.Full):
        log_queue.put_nowait(logging.makeLogRecord({"levelno": logging.INFO, "msg": "info-2"}))


def test_setup_uses_queue_handler_and_prod_only_keeps_file_sink(tmp_path) -> None:
    """验证生产环境只保留文件 sink，根日志器只挂队列处理器。"""
    # 复用单例前先关闭，避免受前序测试污染。
    manager = LoggerManager()
    manager.shutdown()

    # 构造生产环境需要的最小配置对象。
    settings = SimpleNamespace(
        log_level="info",
        app_env="prod",
        is_dev=False,
        log_dir=str(tmp_path),
        log_file_name="app",
        log_max_bytes=1024,
        log_queue_maxsize=8,
    )

    # 执行初始化。
    manager.setup(settings)

    # 验证根日志器只保留一个队列处理器，不再直接绑定真实 sink。
    root_logger = logging.getLogger()
    assert len(root_logger.handlers) == 1
    assert isinstance(root_logger.handlers[0], NonBlockingQueueHandler)

    # 验证后台 listener 已创建，且生产环境只保留文件 sink。
    assert manager._queue_listener is not None
    assert len(manager._sink_handlers) == 1
    assert isinstance(manager._sink_handlers[0], SizeTimedRotatingFileHandler)

    # 测试结束后显式关闭，释放线程和文件句柄。
    manager.shutdown()


def test_setup_uses_queue_handler_and_dev_keeps_console_and_file_sinks(tmp_path) -> None:
    """验证开发环境同时保留控制台 sink 和文件 sink。"""
    # 复用单例前先关闭，避免受前序测试污染。
    manager = LoggerManager()
    manager.shutdown()

    # 构造开发环境需要的最小配置对象。
    settings = SimpleNamespace(
        log_level="info",
        app_env="dev",
        is_dev=True,
        log_dir=str(tmp_path),
        log_file_name="app",
        log_max_bytes=1024,
        log_queue_maxsize=8,
    )

    # 执行初始化。
    manager.setup(settings)

    # 验证根日志器仍只挂队列处理器。
    root_logger = logging.getLogger()
    assert len(root_logger.handlers) == 1
    assert isinstance(root_logger.handlers[0], NonBlockingQueueHandler)

    # 验证开发环境保留两个真实 sink：控制台 + 文件。
    assert manager._queue_listener is not None
    assert len(manager._sink_handlers) == 2
    assert any(isinstance(handler, SizeTimedRotatingFileHandler) for handler in manager._sink_handlers)
    assert any(isinstance(handler, logging.StreamHandler) for handler in manager._sink_handlers)

    # 测试结束后显式关闭，释放线程和文件句柄。
    manager.shutdown()
