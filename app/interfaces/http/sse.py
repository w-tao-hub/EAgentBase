"""SSE (Server-Sent Events) 编码工具。

将业务事件流（AsyncIterator[Event]）编码为符合 SSE 协议的文本流。
"""

from __future__ import annotations

import json
from typing import AsyncIterator

from app.core.models.event import Event


async def encode_sse(event_iterator: AsyncIterator[Event]) -> AsyncIterator[str]:
    """将事件异步迭代器编码为 SSE 格式的文本流。

    SSE 协议格式为：
        event: {event_name}\\n
        data: {json_payload}\\n
        \\n

    对于 RequestFailedEvent，也通过 SSE 事件发出，而不是作为 HTTP 错误返回。

    Args:
        event_iterator: 业务事件异步迭代器

    Yields:
        符合 SSE 协议格式的文本块
    """
    async for event in event_iterator:
        name = event.event_name
        payload = event.to_payload()
        data = json.dumps(payload, ensure_ascii=False)

        yield f"event: {name}\ndata: {data}\n\n"
