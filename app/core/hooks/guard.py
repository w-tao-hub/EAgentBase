"""流式文本守卫定义。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.models.execution_context import ExecutionContext


class StreamTextGuard:
    """流式文本守卫接口。

    当前仅作为未来流式内容审查/打码的扩展点预留。
    默认实现直接透传文本，不做任何业务处理。
    """

    async def ingest_text(self, chunk: str, context: "ExecutionContext") -> list[str]:
        """处理单个文本分片。

        默认直接返回原始 chunk。
        """
        return [chunk]

    async def flush(self, context: "ExecutionContext") -> list[str]:
        """在流结束时返回剩余待输出文本。

        默认无残留文本需要输出。
        """
        return []


class NoOpStreamTextGuard(StreamTextGuard):
    """默认 no-op 流式文本守卫。"""
