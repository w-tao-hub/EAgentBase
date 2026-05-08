"""SSE 事件解析辅助工具。

提供解析 SSE 响应流的工具函数，用于集成测试中解析服务端推送事件。
"""

from __future__ import annotations  # 启用未来注解

import json  # 导入 JSON 解析模块
from typing import Any  # 导入任意类型

from httpx import AsyncClient  # 导入异步 HTTP 客户端


async def collect_sse_events(
    client: AsyncClient,  # 异步 HTTP 客户端
    url: str,  # 请求 URL
    **kwargs: Any,  # 额外请求参数
) -> list[dict]:  # 返回解析后的事件列表
    """发送请求并收集 SSE 响应中的所有事件。

    向指定 URL 发送请求，解析 SSE 响应流，
    将每个事件转换为 {event, data} 格式的字典。

    Args:
        client: 异步 HTTP 客户端实例
        url: 请求 URL
        **kwargs: 额外的请求参数（如 json, params 等）

    Returns:
        解析后的事件列表，每个元素是 {event: str, data: dict} 格式的字典
    """
    # 发送请求并获取原始响应
    # 使用 stream=True 获取流式响应
    events: list[dict] = []  # 初始化事件列表

    # 使用 httpx 的 stream 方法获取流式响应
    async with client.stream("POST", url, **kwargs) as response:  # 发送流式请求
        # 读取完整响应文本
        content = await response.aread()  # 读取全部响应内容
        text = content.decode("utf-8")  # 解码为字符串

    # 按 SSE 协议格式解析事件
    # SSE 格式为 "event: xxx\\ndata: yyy\\n\\n"
    # 用双换行符分割各个事件块
    blocks = text.split("\n\n")  # 按双换行分割事件块

    for block in blocks:  # 遍历每个事件块
        block = block.strip()  # 去除首尾空白
        if not block:  # 空块跳过
            continue  # 跳过

        event_name = ""  # 事件名称
        data_str = ""  # 事件数据

        # 按行解析事件块
        for line in block.split("\n"):  # 遍历每一行
            line = line.strip()  # 去除首尾空白
            if line.startswith("event:"):  # event 行
                event_name = line[len("event:"):].strip()  # 提取事件名称
            elif line.startswith("data:"):  # data 行
                data_str = line[len("data:"):].strip()  # 提取数据

        # 如果有事件名称和数据，则添加到结果列表
        if event_name and data_str:  # 有效事件
            try:  # 尝试解析 JSON
                data = json.loads(data_str)  # 解析 JSON 数据
            except json.JSONDecodeError:  # JSON 解析失败
                data = {"raw": data_str}  # 保留原始数据
            events.append({"event": event_name, "data": data})  # 添加到结果列表

    return events  # 返回解析后的事件列表
