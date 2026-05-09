"""SSE 事件解析辅助工具。

提供解析 SSE 响应流的工具函数，用于集成测试中解析服务端推送事件。
"""

from __future__ import annotations

import json
from typing import Any

from httpx import AsyncClient


async def collect_sse_events(
    client: AsyncClient,
    url: str,
    **kwargs: Any,
) -> list[dict]:
    """发送请求并收集 SSE 响应中的所有事件。"""
    events: list[dict] = []

    async with client.stream("POST", url, **kwargs) as response:
        content = await response.aread()
        text = content.decode("utf-8")

    blocks = text.split("\n\n")

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        event_name = ""
        data_str = ""

        for line in block.split("\n"):
            line = line.strip()
            if line.startswith("event:"):
                event_name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_str = line[len("data:"):].strip()

        if event_name and data_str:
            try:
                data = json.loads(data_str)
            except json.JSONDecodeError:
                data = {"raw": data_str}
            events.append({"event": event_name, "data": data})

    return events
