"""AgentRuntime 实现。

提供单次 LLM 调用的流式执行语义，职责收窄为单次调用。
不再发射 Event，改为 yield str 和 TurnComplete。
"""

from __future__ import annotations  # # 启用未来注解

import asyncio  # # 导入异步模块，用于取消异常
from dataclasses import dataclass  # # 导入数据类装饰器
import logging  # # 导入标准库日志模块，避免 core 反向依赖 infra
from typing import AsyncIterator, TYPE_CHECKING  # # 导入异步迭代器类型和类型检查标记
import uuid  # 导入 UUID 生成模块
from app.core.hooks import (  # # 导入 Hook 相关抽象
    ModelHookPipeline,
    ModelRequest,
    ModelResponse,
    NoOpStreamTextGuard,
    StreamTextGuard,
)

# # 获取模块级日志器。
# # 直接使用标准库 logging，保持 core 层不依赖 infra 包路径。
logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # # 仅在类型检查阶段导入，避免循环依赖
    from app.core.models.execution_context import ExecutionContext  # # 导入执行上下文类型


@dataclass  # # 定义为数据类
class UsageInfo:
    """Token 用量信息。

    封装单次 LLM 调用的 token 消耗统计。
    """

    prompt_tokens: int  # # 输入 token 数量
    completion_tokens: int  # # 输出 token 数量
    total_tokens: int  # # 总 token 数量


@dataclass  # # 定义为数据类
class ToolCall:
    """工具调用定义。

    表示 LLM 请求调用一个工具，采用 OpenAI 兼容的嵌套格式。
    """

    id: str  # # 工具调用唯一标识
    type: str  # # 工具类型，通常为 "function"
    function: Function  # # 函数调用信息


@dataclass  # # 定义为数据类
class Function:
    """函数调用信息。

    嵌套在 ToolCall 内，包含函数名和参数。
    """

    name: str  # # 函数名称
    arguments: str  # # 参数 JSON 字符串


@dataclass  # # 定义为数据类
class TurnComplete:
    """单次 LLM 调用完成标记。

    在流式输出的最后 yield，携带工具调用列表和 Token 用量信息。
    不包含文本内容，文本内容由调用方通过累加 yield 的 str 获得。
    """

    tool_calls: list[ToolCall] | None = None  # # 工具调用列表，无工具调用时为 None
    usage: UsageInfo | None = None  # # Token 用量信息
    reasoning_content: str | None = None  # # 本轮累计的思考内容，供后续工具轮次与历史回放使用


class AgentRuntime:
    """Agent 运行时。

    职责收窄为单次 LLM 调用，不再负责事件流管理。
    主要功能：
    1. 流式调用 LLM，yield str（文本片段）
    2. 累积 tool_call 碎片
    3. 提取 Token 用量信息
    4. 最后 yield TurnComplete（完成标记）
    5. 瞬态错误重试（1-2 次）
    """

    def __init__(
        self,
        llm_adapter: object,
        model_hook_pipeline: ModelHookPipeline | None = None,
        stream_text_guard: StreamTextGuard | None = None,
    ) -> None:  # # 构造函数
        """初始化 AgentRuntime。

        Args:
            llm_adapter: LLM 适配器实例，必须实现 stream_completion 方法
            model_hook_pipeline: 模型 Hook 串行执行管线，未提供时使用空管线
            stream_text_guard: 流式文本守卫，未提供时使用 no-op 守卫
        """
        self._llm_adapter = llm_adapter  # # 保存 LLM 适配器引用
        self._model_hook_pipeline = model_hook_pipeline or ModelHookPipeline()  # # 保存模型 Hook 管线，默认空链
        self._stream_text_guard = stream_text_guard or NoOpStreamTextGuard()  # # 保存流式文本守卫，默认 no-op
        self._max_retries = 2  # # 最大重试次数（初始 1 次 + 重试 1-2 次）

    async def stream_once(  # # 单次流式调用方法
        self,
        agent: object,  # # Agent 配置对象
        messages: list[dict],  # # 已准备好的 LLM 消息列表
        tools: list[dict] | None = None,  # # 可选的工具列表
        context: "ExecutionContext | None" = None,  # # 执行上下文，供 Hook 与文本守卫使用
    ) -> AsyncIterator[str | TurnComplete]:  # # 返回 str 或 TurnComplete 的异步迭代器
        """执行单次 LLM 调用，流式返回 str，最后返回 TurnComplete。

        Args:
            agent: Agent 配置，包含 model、temperature 等属性
            messages: 已经准备完成的 LLM 消息列表
            tools: 可选的工具列表，用于工具调用场景
            context: 执行上下文，未提供时表示当前调用不使用 Hook 扩展语义

        Yields:
            str: 流式文本片段
            TurnComplete: 最终完成标记，包含 tool_calls 和 usage

        Raises:
            Exception: 当重试次数耗尽后仍然失败时抛出异常
        """
        last_error = None  # # 记录最后一次错误

        # # 尝试调用，支持重试
        for attempt in range(self._max_retries):  # # 遍历重试次数
            try:  # # 尝试执行
                async for item in self._do_stream_once(agent, messages, tools, context):  # # 调用实际流式方法
                    yield item  # # 向上游 yield 结果
                return  # # 成功完成，结束方法
            except Exception as e:  # # 捕获异常
                last_error = e  # # 记录错误
                error_msg = str(e).lower()  # # 获取小写错误消息

                # # 如果是取消异常，直接重新抛出，不进行重试
                if isinstance(e, asyncio.CancelledError):  # # 取消异常属于控制流，不应被重试逻辑吞掉
                    raise

                # # 判断是否为瞬态错误（连接错误、超时等）
                is_transient = any(  # # 检查是否为瞬态错误
                    keyword in error_msg  # # 检查错误消息中是否包含关键字
                    for keyword in [  # # 瞬态错误关键字列表
                        "connection",  # # 连接错误
                        "timeout",  # # 超时错误
                        "reset",  # # 连接重置
                        "refused",  # # 连接被拒绝
                        "temporarily",  # # 临时错误
                        "unavailable",  # # 服务不可用
                        "rate limit",  # # 速率限制
                        "503",  # # HTTP 503 错误码
                        "502",  # # HTTP 502 错误码
                        "504",  # # HTTP 504 错误码
                    ]
                )

                if is_transient and attempt < self._max_retries - 1:  # # 如果是瞬态错误且还有重试次数
                    logger.warning(  # # 记录警告日志
                        "LLM 调用遇到瞬态错误，准备重试: attempt=%d, error=%s",  # # 日志消息
                        attempt + 1,  # # 当前尝试次数
                        e,  # # 错误信息
                    )
                    continue  # # 继续下一次重试
                else:  # # 非瞬态错误或重试次数耗尽
                    raise  # # 抛出异常

        # # 如果执行到这里，说明重试次数已耗尽
        if last_error:  # # 如果有记录的错误
            raise last_error  # # 抛出最后一次错误

    async def _do_stream_once(  # # 实际执行单次流式调用的内部方法
        self,
        agent: object,  # # Agent 配置对象
        messages: list[dict],  # # 已准备好的 LLM 消息列表
        tools: list[dict] | None = None,  # # 可选的工具列表
        context: "ExecutionContext | None" = None,  # # 执行上下文，供 Hook 与文本守卫使用
    ) -> AsyncIterator[str | TurnComplete]:  # # 返回 str 或 TurnComplete 的异步迭代器
        """实际执行单次 LLM 调用的内部方法。

        Args:
            agent: Agent 配置
            messages: LLM 消息列表
            tools: 可选的工具列表

        Yields:
            str: 流式文本片段
            TurnComplete: 最终完成标记
        """
        # # 初始化累积变量
        text_parts: list[str] = []  # # 文本片段列表，保存真正已经向上游发出的文本
        reasoning_parts: list[str] = []  # # 思考内容片段列表，只累计不直接向用户输出
        # # 工具调用累积器，key 使用 tool_call 的 index。
        # # 真实 OpenAI / LiteLLM 增量流里，后续片段往往只携带 index，
        # # 不会重复携带 id / function_name，因此必须按 index 归并，不能只按 id 归并。
        tool_call_accumulator: dict[int, dict] = {}
        usage_info: UsageInfo | None = None  # # Token 用量信息

        # # 构造模型请求对象，并通过 before_model Hook 串行改写。
        model_request = ModelRequest(
            messages=list(messages),  # # 复制消息列表，避免 Hook 直接污染调用方持有的对象
            tools=list(tools) if tools is not None else None,  # # 复制工具列表，允许 Hook 安全改写
            model=agent.model,  # # 当前模型名
            temperature=agent.temperature,  # # 当前温度参数
        )
        if context is not None:  # # 只有存在执行上下文时才执行 Hook 管线
            model_request = await self._model_hook_pipeline.before_model(model_request, context)  # # 执行 before_model Hook 链

        # # 调用 LLM 适配器
        async for chunk in self._llm_adapter.stream_completion(  # # 遍历 LLM 返回的 chunks
            model=model_request.model,  # # 模型名称，允许被 Hook 改写
            messages=model_request.messages,  # # 消息列表，允许被 Hook 改写
            temperature=model_request.temperature,  # # 温度参数，允许被 Hook 改写
            api_key=None,  # # API 密钥（None 表示由适配器从环境变量读取）
            tools=model_request.tools,  # # 工具列表，允许被 Hook 改写
            reasoning_effort=agent.reasoning_effort,  # # DeepSeek thinking 模式的思考强度固定来自 Agent 静态配置
        ):
            # # 检查取消信号，若已设置则主动抛出 CancelledError 中断流式读取
            if context is not None and context.cancel_event.is_set():
                raise asyncio.CancelledError("Run cancelled during streaming")

            # # 提取思考内容。
            # # 这部分只用于后续继续推理与历史回放，不直接向用户输出。
            thinking = self._extract_thinking(chunk)  # # 从 chunk 中提取 reasoning / thinking 增量
            if thinking:  # # 仅在当前分片确实携带思考内容时才累计
                reasoning_parts.append(thinking)  # # 追加到本轮思考内容聚合结果里

            # # 提取文本内容
            content = self._extract_content(chunk)  # # 从 chunk 中提取文本内容
            if content:  # # 如果内容不为空
                guarded_chunks = [content]  # # 默认直接透传原始文本分片
                if context is not None:  # # 有执行上下文时才走守卫接口，便于后续扩展
                    guarded_chunks = await self._stream_text_guard.ingest_text(content, context)  # # 交给文本守卫处理
                for guarded_chunk in guarded_chunks:  # # 逐个输出守卫返回的文本片段
                    text_parts.append(guarded_chunk)  # # 记录真实输出文本，保持最终聚合与流式一致
                    yield guarded_chunk  # # yield 处理后的文本片段

            # # 提取并累积 tool_calls
            tool_call_deltas = self._extract_tool_calls(chunk)  # # 从 chunk 中提取工具调用增量
            for tc_delta in tool_call_deltas:  # # 遍历工具调用增量
                tc_id = tc_delta.get("id")  # # 获取工具调用 ID（可能为 None）
                tc_index = tc_delta.get("index", 0)  # # 获取工具调用索引

                if tc_index not in tool_call_accumulator:  # # 如果是新的工具调用
                    tool_call_accumulator[tc_index] = {  # # 初始化累积器
                        "id": tc_id or "",  # # 工具调用 ID（可能暂时为空）
                        "name": "",  # # 工具名称
                        "arguments": "",  # # 工具参数
                    }

                # # 后续增量片段可能补上 id，因此在已有累积器上持续回填。
                if tc_id:  # # 如果当前增量携带了 id
                    tool_call_accumulator[tc_index]["id"] = tc_id  # # 更新工具调用 ID

                # # 累积工具名称
                if tc_delta.get("function_name"):  # # 如果有工具名称
                    tool_call_accumulator[tc_index]["name"] = tc_delta["function_name"]  # # 更新工具名称
                # # 兼容旧的 name 字段（如果有的话）
                elif tc_delta.get("name"):  # # 兼容旧格式
                    tool_call_accumulator[tc_index]["name"] = tc_delta["name"]  # # 更新工具名称

                # # 累积参数
                if tc_delta.get("arguments"):  # # 如果有参数
                    tool_call_accumulator[tc_index]["arguments"] += tc_delta["arguments"]  # # 追加参数

            # # 提取 usage
            chunk_usage = self._extract_usage(chunk)  # # 提取用量信息
            if chunk_usage:  # # 如果有用量信息
                usage_info = chunk_usage  # # 更新用量信息

        # # 流结束后给守卫一次 flush 机会。
        if context is not None:  # # 只有存在执行上下文时才调用守卫
            for guarded_chunk in await self._stream_text_guard.flush(context):  # # 处理守卫残留文本
                text_parts.append(guarded_chunk)  # # 记录真实输出文本
                yield guarded_chunk  # # 输出守卫残留文本

        # # 构建 tool_calls 列表
        tool_calls: list[ToolCall] | None = None  # # 初始化工具调用列表
        if tool_call_accumulator:  # # 如果有累积的工具调用
            tool_calls = [  # # 构建 ToolCall 对象列表
                ToolCall(  # # 创建 ToolCall 对象（OpenAI 兼容格式）,对缺失 id 的情况进行防御处理
                    id=(
                        tc["id"]
                        if tc["id"]
                        else (
                            logger.warning(f"工具调用片段缺少 id,已生成临时 id: index={str(uuid.uuid4())}"),
                            f"auto-generated-call-{str(uuid.uuid4())}",
                        )[1]
                    ),  # # 工具调用 ID
                    type="function",  # # 类型固定为 function
                    function=Function(  # # 嵌套函数信息
                        name=tc["name"],  # # 函数名
                        arguments=tc["arguments"],  # # 函数参数
                    ),
                )
                for _, tc in sorted(tool_call_accumulator.items())  # # 按 index 顺序输出工具调用
            ]

        # # 构造模型响应对象，并通过 after_model Hook 串行改写。
        model_response = ModelResponse(
            text="".join(text_parts),  # # 使用真实已输出文本构造完整响应
            tool_calls=tool_calls,  # # 传入本轮归并出的工具调用列表
            usage=usage_info,  # # 传入当前用量信息
        )
        if context is not None:  # # 只有存在执行上下文时才执行 after_model Hook 链
            model_response = await self._model_hook_pipeline.after_model(model_response, context)  # # 执行 after_model Hook 链

        # # 构建并返回 TurnComplete。
        # # 注意：after_model 若改写 text，不会回放已流出的 chunk；当前仅对 tool_calls / usage 生效。
        yield TurnComplete(  # # yield TurnComplete（完成标记）
            tool_calls=model_response.tool_calls,  # # 工具调用列表，允许被 Hook 改写
            usage=model_response.usage,  # # Token 用量信息，允许被 Hook 改写
            reasoning_content="".join(reasoning_parts) if reasoning_parts else None,  # # 只有存在思考内容时才返回聚合后的 reasoning_content
        )

    def _extract_content(self, chunk: object) -> str | None:  # # 从 chunk 中提取文本内容
        """从 LLMChunk 中提取文本内容。

        Args:
            chunk: LLMChunk 对象

        Returns:
            提取的文本内容，如果不存在则返回 None
        """
        # # LLMChunk 直接提供 content 属性
        return chunk.content if hasattr(chunk, "content") else None  # # 直接返回 content

    def _extract_tool_calls(self, chunk: object) -> list[dict]:  # # 从 chunk 中提取工具调用增量
        """从 LLMChunk 中提取工具调用增量。

        Args:
            chunk: LLMChunk 对象

        Returns:
            工具调用增量列表，每个元素包含 index、id、function_name、arguments
        """
        # # LLMChunk 直接提供 tool_calls 属性（字典列表）
        tool_calls_attr = getattr(chunk, "tool_calls", None)
        if tool_calls_attr is None:
            return []
        return tool_calls_attr if isinstance(tool_calls_attr, list) else []

    def _extract_thinking(self, chunk: object) -> str | None:  # # 从 chunk 中提取思考内容
        """从 LLMChunk 中提取 reasoning / thinking 文本。

        Args:
            chunk: LLMChunk 对象

        Returns:
            当前 chunk 携带的思考内容；不存在时返回 None
        """
        return chunk.thinking if hasattr(chunk, "thinking") else None  # # LLMChunk 已经把 provider 差异收敛到 thinking 字段

    def _extract_usage(self, chunk: object) -> UsageInfo | None:  # # 从 chunk 中提取用量信息
        """从 LLMChunk 中提取 Token 用量信息。

        Args:
            chunk: LLMChunk 对象

        Returns:
            UsageInfo 对象，如果不存在则返回 None
        """
        # # LLMChunk 现在直接提供 UsageInfo 类型的 usage 属性，无需转换
        return chunk.usage if hasattr(chunk, "usage") else None  # # 直接返回 usage 属性
