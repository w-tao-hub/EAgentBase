"""聊天运行锁作用域。

把原先散落在 ChatService 中的 acquire/release 逻辑封装为
异步上下文管理器，使锁的生命周期边界更加清晰。
"""

from __future__ import annotations  # 启用未来注解，方便类型引用

import asyncio  # 导入 asyncio，用于后台心跳任务与取消控制
import logging  # 导入标准库日志，保持 services 层不依赖 infra 具体实现
from typing import TYPE_CHECKING, Any, Awaitable  # 导入类型检查标记与异步类型，避免运行时循环依赖

logger = logging.getLogger(__name__)  # 创建模块级日志器，记录锁获取与释放过程

if TYPE_CHECKING:  # 仅在类型检查时导入锁存储类型
    from app.infra.store.redis_lock_store import RedisLockStore  # Redis 锁存储类型


class ChatRunLockNotAcquiredError(Exception):
    """聊天运行锁获取失败异常。

    当某个 session 已经存在活跃 run 时，
    上下文管理器会抛出该异常，让调用方把它收敛为 request_failed 事件。
    """

    def __init__(self, session_id: str, run_id: str) -> None:  # 构造函数
        """初始化异常对象。"""
        self.session_id = session_id  # 保存会话 ID，便于上层记录日志与生成错误事件
        self.run_id = run_id  # 保存当前尝试获取锁的 run_id
        super().__init__(f"Failed to acquire chat run lock: session_id={session_id}, run_id={run_id}")


class ChatRunLockHeartbeatLostError(Exception):
    """聊天运行锁心跳失效异常。

    当后台续期检测到锁 owner 已丢失，或 Redis 续期过程本身发生异常时，
    通过该异常通知业务层将当前 run 收敛为失败态。
    """

    def __init__(self, session_id: str, run_id: str, reason: str) -> None:  # 构造函数
        """初始化异常对象。"""
        self.session_id = session_id  # 保存会话 ID，便于业务层定位问题
        self.run_id = run_id  # 保存失锁对应的运行 ID
        self.reason = reason  # 保存具体失败原因，便于日志与终态消息输出
        super().__init__(f"Chat run lock heartbeat lost: session_id={session_id}, run_id={run_id}, reason={reason}")


class ChatRunLockScope:
    """聊天运行锁作用域。

    进入作用域时尝试获取锁，离开作用域时统一释放锁。
    该对象不负责生成业务事件，只负责资源生命周期管理。
    """

    def __init__(
        self,
        lock_store: RedisLockStore,  # 锁存储实例
        session_id: str,  # 会话 ID
        run_id: str,  # 当前运行 ID
        ttl_seconds: int,  # 锁 TTL 秒数
    ) -> None:
        """初始化锁作用域。"""
        self._lock_store = lock_store  # 保存锁存储，供进入/退出作用域时调用
        self._session_id = session_id  # 保存会话 ID
        self._run_id = run_id  # 保存运行 ID
        self._ttl_seconds = ttl_seconds  # 保存锁 TTL
        self._acquired = False  # 记录当前作用域是否已成功持有锁
        # 心跳间隔固定为 TTL 的一半，既留出安全余量，又避免续期过于频繁
        self._heartbeat_interval_seconds = max(1.0, ttl_seconds / 2)  # 保存后台心跳间隔
        self._heartbeat_task: asyncio.Task[None] | None = None  # 保存后台心跳任务引用
        self._stop_event: asyncio.Event | None = None  # 停止事件，用于优雅结束心跳循环
        self._owner_task: asyncio.Task[Any] | None = None  # 保存持锁主协程任务，便于失锁时取消
        self._heartbeat_error: ChatRunLockHeartbeatLostError | None = None  # 保存后台心跳探测到的失锁原因

    async def __aenter__(self) -> "ChatRunLockScope":
        """进入锁作用域。

        Returns:
            ChatRunLockScope: 返回自身，便于未来扩展更多只读状态

        Raises:
            ChatRunLockNotAcquiredError: 当锁已被其他运行占用时抛出
        """
        self._acquired = await self._lock_store.acquire(
            session_id=self._session_id,  # 传入会话 ID，按会话维度互斥
            run_id=self._run_id,  # 使用当前 run_id 作为锁 owner
            ttl_seconds=self._ttl_seconds,  # 使用配置的锁 TTL
        )
        if not self._acquired:  # 如果获取失败，抛出受控异常，让业务层转成 request_failed
            logger.error("会话锁获取失败，存在活跃 Run: session_id=%s", self._session_id)
            raise ChatRunLockNotAcquiredError(self._session_id, self._run_id)

        # 抢锁成功后立即启动后台心跳，以覆盖后续整个 run 生命周期
        self._owner_task = asyncio.current_task()  # 记录当前持锁主任务，便于后台失锁时打断主流程
        self._stop_event = asyncio.Event()  # 创建停止事件，供退出作用域时通知心跳循环结束
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(),
            name=f"chat-run-lock-heartbeat:{self._session_id}:{self._run_id}",
        )

        logger.info("会话锁获取成功: session_id=%s, run_id=%s", self._session_id, self._run_id)
        return self  # 返回自身，便于以后扩展更多上下文信息

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        """退出锁作用域。

        无论作用域内部是正常结束还是抛出异常，都会尝试释放锁。

        Returns:
            bool: 始终返回 False，不吞掉业务异常
        """
        if not self._acquired:  # 如果此前根本没有拿到锁，则无需释放
            return False

        cleanup_cancelled = False  # 记录清理阶段是否又收到了新的取消信号

        try:  # 清理阶段要尽量完整执行，避免心跳任务泄漏或锁残留
            try:  # 先停心跳，避免与 release 竞争
                await self._run_cleanup(self._stop_heartbeat())
            except asyncio.CancelledError:  # 清理期间若再次被取消，先记账，继续完成剩余清理
                cleanup_cancelled = True  # 标记清理期间发生过额外取消

            try:  # 再执行真正的锁释放，确保即使外层取消也不会遗留锁
                await self._run_cleanup(self._release_lock())
            except asyncio.CancelledError:  # 同样记录二次取消，但不跳过释放后的收尾逻辑
                cleanup_cancelled = True  # 标记清理期间发生过额外取消
        finally:  # 无论结果如何，都将本地状态重置，避免重复释放
            self._acquired = False  # 标记当前作用域已经不再持锁
            self._owner_task = None  # 清理主任务引用，避免悬挂引用
            self._stop_event = None  # 清理停止事件引用
            self._heartbeat_task = None  # 清理心跳任务引用

        # 如果本次退出是由后台心跳取消主任务触发，则把 CancelledError 转换成受控业务异常
        if exc_type is asyncio.CancelledError and self._heartbeat_error is not None:
            raise self._heartbeat_error from exc
        # 如果不是心跳失锁，而是清理阶段额外收到取消，则把取消继续交回上层。
        if cleanup_cancelled:
            raise asyncio.CancelledError
        return False  # 不吞掉作用域内部异常

    async def _heartbeat_loop(self) -> None:
        """后台心跳循环。

        只要作用域仍持锁，就按固定间隔执行 owner 校验续期。
        一旦续期失败，则取消主任务，让业务层尽快收敛当前 run。
        """
        stop_event = self._stop_event  # 读取当前停止事件，便于循环内重复使用
        if stop_event is None:  # 理论上不会发生，这里做保护性返回
            return

        while True:  # 持续续期直到收到停止信号或检测到失锁
            try:  # 先等待“停止”或“到达下一个续期时间点”
                await asyncio.wait_for(stop_event.wait(), timeout=self._heartbeat_interval_seconds)
                return  # 收到停止信号后直接退出心跳循环
            except asyncio.TimeoutError:  # 正常超时表示到了应续期的时刻
                pass  # 继续执行下面的续期逻辑
            except asyncio.CancelledError:  # 外部若直接取消心跳任务，按协程约定继续抛出
                raise

            try:  # 执行 owner 校验续期
                extended = await self._lock_store.extend(
                    session_id=self._session_id,
                    run_id=self._run_id,
                    ttl_seconds=self._ttl_seconds,
                )
            except Exception as error:  # Redis 续期异常也视为当前 run 已无法可信持锁
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
                self._cancel_owner_task()  # 通过取消主任务触发作用域尽快退出
                return

            if extended:  # 当前 run 仍是 owner，继续等待下一次心跳即可
                logger.debug("会话锁心跳续期成功: session_id=%s, run_id=%s", self._session_id, self._run_id)
                continue

            logger.error("会话锁心跳续期失败，当前 run 已失去 owner: session_id=%s, run_id=%s", self._session_id, self._run_id)
            self._heartbeat_error = ChatRunLockHeartbeatLostError(
                session_id=self._session_id,
                run_id=self._run_id,
                reason="lock owner mismatch or lock already expired",
            )
            self._cancel_owner_task()  # 检测到失锁后立刻打断主协程，避免继续执行
            return

    def _cancel_owner_task(self) -> None:
        """取消持锁主任务，让业务层尽快感知失锁。"""
        owner_task = self._owner_task  # 读取当前主任务引用
        if owner_task is None:  # 若没有主任务引用，说明无需打断
            return
        if owner_task.done():  # 已结束的任务不再重复取消
            return
        if owner_task is asyncio.current_task():  # 防止心跳任务错误地取消自己
            return
        owner_task.cancel()  # 取消主任务，驱动作用域进入清理分支

    async def _stop_heartbeat(self) -> None:
        """停止后台心跳任务并等待其退出。"""
        stop_event = self._stop_event  # 读取停止事件
        heartbeat_task = self._heartbeat_task  # 读取心跳任务引用
        if stop_event is None or heartbeat_task is None:  # 没有启动心跳时直接返回
            return
        stop_event.set()  # 通知心跳循环结束
        await heartbeat_task  # 等待心跳任务真正退出，避免后台残留

    async def _release_lock(self) -> None:
        """执行实际的锁释放逻辑。"""
        try:  # 尝试释放锁，保持与旧实现一致：释放失败不再向上抛错
            released = await self._lock_store.release(self._session_id, self._run_id)
            if released:  # 正常释放成功
                logger.info("会话锁已释放: session_id=%s, run_id=%s", self._session_id, self._run_id)
            else:  # 锁不存在或 owner 已变化时，记录警告帮助排查问题
                logger.warning(
                    "会话锁释放未生效: session_id=%s, run_id=%s",
                    self._session_id,
                    self._run_id,
                )
        except Exception:  # 释放阶段异常仅记录日志，不覆盖原始业务异常
            logger.exception(
                "释放会话锁时发生异常: session_id=%s, run_id=%s",
                self._session_id,
                self._run_id,
            )

    async def _run_cleanup(self, operation: Awaitable[None]) -> None:
        """在取消语境下可靠执行清理动作。

        使用 shield 保护清理协程本身不被外层取消打断；
        如果外层已经处于取消态，则等待清理完成后再把取消继续向上传递。
        """
        cleanup_task = asyncio.create_task(operation)  # 单独创建任务，使 shield 能保护真实清理逻辑
        try:  # 先在普通情况下等待清理结束
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:  # 若外层主任务已被取消，仍要等待清理动作完成
            await cleanup_task  # 等待真实清理完成后，再把取消交回上层处理
            raise
