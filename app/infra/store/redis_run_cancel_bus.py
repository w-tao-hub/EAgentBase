"""Redis 运行取消广播适配器。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis


class RedisRunCancelBus:
    """基于 Redis pubsub 的 Run 取消广播适配器。"""

    def __init__(self, redis: "Redis") -> None:
        """保存 pubsub 专用 Redis 客户端。"""
        self._redis = redis
        self._pattern = "run_cancel:*"
        self._prefix = "run_cancel:"
        self._pubsub: object | None = None
        self._listener_task: asyncio.Task[object] | None = None
        self._closing = False

    async def publish_cancel(self, run_id: str) -> None:
        """发布指定 run_id 的取消信号。"""
        await self._redis.publish(f"{self._prefix}{run_id}", "cancel")

    async def listen_cancelled_run_ids(self) -> AsyncIterator[str]:
        """监听取消广播，并逐个产出被取消的 run_id。"""
        self._closing = False
        self._listener_task = asyncio.current_task()
        pubsub = self._redis.pubsub()
        self._pubsub = pubsub
        await pubsub.psubscribe(self._pattern)
        try:
            async for message in pubsub.listen():
                if message.get("type") != "pmessage":
                    continue
                if message.get("data") != "cancel":
                    continue
                run_id = self._extract_run_id(message.get("channel"))
                if run_id is None:
                    continue
                yield run_id
        except asyncio.CancelledError:
            if self._closing:
                return
            raise
        finally:
            await self._close_pubsub(pubsub)
            if self._pubsub is pubsub:
                self._pubsub = None
            if self._listener_task is asyncio.current_task():
                self._listener_task = None

    async def aclose(self) -> None:
        """关闭当前 pubsub 监听资源。"""
        self._closing = True
        pubsub = self._pubsub
        listener_task = self._listener_task
        if pubsub is not None:
            await self._close_pubsub(pubsub)
            if self._pubsub is pubsub:
                self._pubsub = None
        if listener_task is not None and listener_task is not asyncio.current_task() and not listener_task.done():
            listener_task.cancel()

    async def _close_pubsub(self, pubsub: object) -> None:
        """退订并关闭 pubsub。"""
        try:
            await pubsub.punsubscribe(self._pattern)
        finally:
            await pubsub.aclose()

    def _extract_run_id(self, channel: object) -> str | None:
        """从取消频道名中解析 run_id。"""
        if isinstance(channel, bytes):
            channel = channel.decode()
        if not isinstance(channel, str):
            return None
        if not channel.startswith(self._prefix):
            return None
        run_id = channel.removeprefix(self._prefix)
        return run_id or None
