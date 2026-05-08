"""SSE (Server-Sent Events) 编码工具。

将业务事件流（AsyncIterator[Event]）编码为符合 SSE 协议的文本流。
"""

from __future__ import annotations  # 启用未来注解

import json  # 导入 JSON 序列化模块
from typing import AsyncIterator  # 导入异步迭代器类型

from app.core.models.event import Event  # 导入事件基类


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
    # 遍历事件流中的每一个事件
    async for event in event_iterator:
        # 获取事件名称，用于 SSE event 字段
        name = event.event_name  # 事件名称

        # 将事件 payload 序列化为 JSON 字符串
        payload = event.to_payload()  # 获取事件 payload 字典
        data = json.dumps(payload, ensure_ascii=False)  # 序列化为 JSON

        # 按照 SSE 协议格式拼接文本块
        # event: 行指定事件类型
        # data: 行携带事件数据
        # 最后追加空行表示事件结束
        yield f"event: {name}\ndata: {data}\n\n"
