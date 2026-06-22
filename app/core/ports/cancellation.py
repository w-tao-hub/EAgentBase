"""运行取消广播端口。

跨进程的 Run 取消需要一种通信机制：当用户在进程 A 取消一个 Run 时，
如果该 Run 实际运行在进程 B 上（多进程部署场景），进程 A 无法直接
打断进程 B 的执行。该端口抽象了这种跨进程取消广播能力——发布端发布
取消信号，监听端在下一个检测点感知到取消并终止执行。

当前已知的底层实现是 Redis Pub/Sub，但端口本身不限定底层机制，
也可以替换为 RabbitMQ、gRPC stream 或其他消息系统。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol


class RunCancelBus(Protocol):
    """跨进程 Run 取消广播端口。

    该端口定义了两个相对的操作：
    - publish_cancel: 生产者发布取消信号
    - listen_cancelled_run_ids: 消费者监听取消信号

    选择 AsyncIterator 作为监听接口的返回类型，是因为取消信号本质
    上是一个持续到达的事件流——调用方通过 async for 循环逐个处理
    被取消的 run_id，不需要手动管理连接生命周期。当调用方不再需要
    监听时，调用 aclose() 关闭底层资源即可。

    Protocol 而非 ABC 的原因：实现方不需要显式继承 RunCancelBus，
    只要存在 publish_cancel、listen_cancelled_run_ids、aclose 三个
    方法且签名匹配，就可以被当作 RunCancelBus 注入到服务层。
    """

    async def publish_cancel(self, run_id: str) -> None:
        """广播指定 run_id 的取消信号。

        实现方应保证 publish 操作的非阻塞性——它只是一个信号通知，
        不应该等待消费方处理完成才返回。因此底层实现推荐使用 Pub/Sub
        或消息队列的"即发即忘"模式。
        """

    def listen_cancelled_run_ids(self) -> AsyncIterator[str]:
        """监听取消信号，并逐个产出被取消的 run_id。

        该方法返回 AsyncIterator[str] 而非 async generator 函数，
        以便实现方可以更灵活地控制内部资源（如连接池、订阅对象等）。
        调用方通过 async for run_id in bus.listen_cancelled_run_ids()
        来持续接收取消事件。
        """

    async def aclose(self) -> None:
        """关闭底层监听资源。

        当业务层不再需要监听取消信号时（如服务器关闭），调用此方法
        主动释放连接和订阅。运行中的 listen_cancelled_run_ids 迭代
        应该在这种情况下优雅退出。
        """
