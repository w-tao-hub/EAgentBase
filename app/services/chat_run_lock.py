"""聊天运行锁作用域。"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Awaitable

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.core.ports.stores import LockStore


class ChatRunLockNotAcquiredError(Exception):
    """聊天运行锁获取失败异常。"""

    def __init__(self, session_id: str, run_id: str) -> None:
        self.session_id = session_id
        self.run_id = run_id
        super().__init__(f"Failed to acquire chat run lock: session_id={session_id}, run_id={run_id}")


class ChatRunLockHeartbeatLostError(Exception):
    """聊天运行锁心跳失效异常。"""

    def __init__(self, session_id: str, run_id: str, reason: str) -> None:
        self.session_id = session_id
        self.run_id = run_id
        self.reason = reason
        super().__init__(f"Chat run lock heartbeat lost: session_id={session_id}, run_id={run_id}, reason={reason}")


class ChatRunLockScope:
    """聊天运行锁作用域。

    进入作用域时尝试获取锁，离开作用域时统一释放锁。
    该对象不负责生成业务事件，只负责资源生命周期管理。
    """

    def __init__(
        self,
        lock_store: "LockStore",
        session_id: str,
        run_id: str,
        ttl_seconds: int,
    ) -> None:
        self._lock_store = lock_store
        self._session_id = session_id
        self._run_id = run_id
        self._ttl_seconds = ttl_seconds
        self._acquired = False
        # 心跳间隔固定为 TTL 的一半，既留出安全余量，又避免续期过于频繁
        self._heartbeat_interval_seconds = max(1.0, ttl_seconds / 2)
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._owner_task: asyncio.Task[Any] | None = None
        self._heartbeat_error: ChatRunLockHeartbeatLostError | None = None

    async def __aenter__(self) -> "ChatRunLockScope":
        """进入锁作用域。"""
        self._acquired = await self._lock_store.acquire(
            session_id=self._session_id,
            run_id=self._run_id,
            ttl_seconds=self._ttl_seconds,
        )
        if not self._acquired:
            logger.error("会话锁获取失败，存在活跃 Run: session_id=%s", self._session_id)
            raise ChatRunLockNotAcquiredError(self._session_id, self._run_id)

        # 抢锁成功后立即启动后台心跳，以覆盖后续整个 run 生命周期
        self._owner_task = asyncio.current_task()
        self._stop_event = asyncio.Event()
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(),
            name=f"chat-run-lock-heartbeat:{self._session_id}:{self._run_id}",
        )

        logger.info("会话锁获取成功: session_id=%s, run_id=%s", self._session_id, self._run_id)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        """退出锁作用域。"""
        if not self._acquired:
            return False

        cleanup_cancelled = False

        try:
            try:
                await self._run_cleanup(self._stop_heartbeat())
            except asyncio.CancelledError:
                cleanup_cancelled = True

            try:
                await self._run_cleanup(self._release_lock())
            except asyncio.CancelledError:
                cleanup_cancelled = True
        finally:
            self._acquired = False
            self._owner_task = None
            self._stop_event = None
            self._heartbeat_task = None

        # 如果本次退出是由后台心跳取消主任务触发，则把 CancelledError 转换成受控业务异常
        if exc_type is asyncio.CancelledError and self._heartbeat_error is not None:
            raise self._heartbeat_error from exc
        if cleanup_cancelled:
            raise asyncio.CancelledError
        return False

    async def _heartbeat_loop(self) -> None:
        """后台心跳循环。"""
        stop_event = self._stop_event
        if stop_event is None:
            return

        while True:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._heartbeat_interval_seconds)
                return
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise

            try:
                extended = await self._lock_store.extend(
                    session_id=self._session_id,
                    run_id=self._run_id,
                    ttl_seconds=self._ttl_seconds,
                )
            except Exception as error:
                logger.exception(
                    "会话锁心跳续期异常: session_id=%s, run_id=%s",
                    self._session_id,
                    self._run_id,
                )
                self._heartbeat_error = ChatRunLockHeartbeatLostError(
                    session_id=self._session_id,
                    run_id=self._run_id,
                    reason=f"heartbeat extend raised error: {error}",
                )
                self._cancel_owner_task()
                return

            if extended:
                logger.debug("会话锁心跳续期成功: session_id=%s, run_id=%s", self._session_id, self._run_id)
                continue

            logger.error("会话锁心跳续期失败，当前 run 已失去 owner: session_id=%s, run_id=%s", self._session_id, self._run_id)
            self._heartbeat_error = ChatRunLockHeartbeatLostError(
                session_id=self._session_id,
                run_id=self._run_id,
                reason="lock owner mismatch or lock already expired",
            )
            self._cancel_owner_task()
            return

    def _cancel_owner_task(self) -> None:
        """取消持锁主任务。"""
        owner_task = self._owner_task
        if owner_task is None:
            return
        if owner_task.done():
            return
        if owner_task is asyncio.current_task():
            return
        owner_task.cancel()

    async def _stop_heartbeat(self) -> None:
        """停止后台心跳任务并等待其退出。"""
        stop_event = self._stop_event
        heartbeat_task = self._heartbeat_task
        if stop_event is None or heartbeat_task is None:
            return
        stop_event.set()
        await heartbeat_task

    async def _release_lock(self) -> None:
        """执行实际的锁释放逻辑。"""
        try:
            released = await self._lock_store.release(self._session_id, self._run_id)
            if released:
                logger.info("会话锁已释放: session_id=%s, run_id=%s", self._session_id, self._run_id)
            else:
                logger.warning(
                    "会话锁释放未生效: session_id=%s, run_id=%s",
                    self._session_id,
                    self._run_id,
                )
        except Exception:
            logger.exception(
                "释放会话锁时发生异常: session_id=%s, run_id=%s",
                self._session_id,
                self._run_id,
            )

    async def _run_cleanup(self, operation: Awaitable[None]) -> None:
        """在取消语境下可靠执行清理动作。"""
        cleanup_task = asyncio.create_task(operation)
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            await cleanup_task
            raise
