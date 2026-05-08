"""LiteLLM 适配器实现。

提供 LiteLLM 的流式调用适配，将 chunk 归一化为统一格式。
"""

from __future__ import annotations  # 启用未来注解

import asyncio  # 导入异步模块，用于将同步的 token_counter 丢入线程池执行，避免阻塞事件循环
from typing import Any, AsyncIterator  # 导入异步迭代器和任意类型

from app.core.models.llm_chunk import LLMChunk  # 导入 LLMChunk 模型
from app.core.runtime.agent_runtime import UsageInfo  # 导入 UsageInfo
from app.infra.logging import get_logger  # 导入日志获取函数

# 获取模块级日志器
logger = get_logger(__name__)


class LiteLLMAdapter:
    """LiteLLM 的适配器实现。

    将 LiteLLM 的流式响应归一化为统一的 LLMChunk 格式。
    处理 LiteLLM 调用中的异常情况。
    支持文本内容和工具调用增量片段的解析。
    """

    def __init__(self, timeout_seconds: int = 60) -> None:  # 构造函数
        """初始化 LiteLLM 适配器。

        Args:
            timeout_seconds: 请求超时时间（秒）
        """
        self._timeout_seconds = timeout_seconds  # 保存超时时间

    async def count_prompt_tokens(self, model: str, messages: list[dict[str, Any]]) -> int:
        """统计当前待发送消息的输入 token 数。"""
        litellm = self._load_litellm()  # 延迟导入 LiteLLM，保持启动时依赖收敛
        litellm.request_timeout = self._timeout_seconds  # 保持所有 LiteLLM 调用共享同一超时配置
        return int(await asyncio.to_thread(litellm.token_counter, model=model, messages=messages))  # 将同步 CPU 密集型 token 计数丢入线程池执行，避免阻塞 asyncio 事件循环

    async def complete_text(
        self,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float,
        api_key: str | None = None,
        enable_thinking: bool = False,
        reasoning_effort: str | None = None,
    ) -> str:
        """执行一次非流式文本补全，并返回完整文本。"""
        litellm = self._load_litellm()  # 延迟导入 LiteLLM，避免模块导入时强依赖
        litellm.request_timeout = self._timeout_seconds  # 保持非流式与流式调用使用同一超时配置
        completion_kwargs = self._build_completion_kwargs(  # 统一复用 DeepSeek / 非 DeepSeek 参数组装逻辑
            model=model,
            messages=messages,
            temperature=temperature,
            api_key=api_key,
            tools=None,
            enable_thinking=enable_thinking,
            reasoning_effort=reasoning_effort,
        )

        response = await litellm.acompletion(**completion_kwargs, stream=False)  # 执行一次非流式补全
        content = self._extract_completion_text(response)  # 从响应对象里提取聚合后的文本
        if content is None:  # 没拿到文本时直接抛错，避免压缩逻辑把空摘要写入上下文
            raise ValueError("LiteLLM 非流式补全未返回可用文本")
        return content

    async def stream_completion(  # 流式完成方法
        self,
        model: str,  # 模型名称
        messages: list[dict[str, str]],  # 消息列表
        temperature: float,  # 温度参数
        api_key: str | None = None,  # API 密钥（None 表示由 LiteLLM 从环境变量读取）
        tools: list[dict] | None = None,  # 工具定义列表（新增参数，用于函数调用）
        reasoning_effort: str | None = None,  # DeepSeek thinking 模式的思考强度
    ) -> AsyncIterator[LLMChunk]:  # 返回异步迭代器，现在返回 LLMChunk
        """调用 LiteLLM 进行流式对话完成。

        Args:
            model: 要调用的模型名称
            messages: 对话消息列表，每个消息是 {"role": str, "content": str}
            temperature: 采样温度，范围 0.0 ~ 2.0
            api_key: LLM API 密钥
            tools: 工具定义列表，用于支持函数调用功能

        Yields:
            归一化后的 LLMChunk 对象，包含 content、tool_calls 和 finish_reason

        Note:
            如果调用过程中发生异常，异常会向上传播到调用方（AgentRuntime），
            由调用方统一收敛为 run_failed 事件。
        """
        # 不在此处捕获异常，让异常自然传播到 AgentRuntime 处理
        litellm = self._load_litellm()  # 延迟导入 LiteLLM，避免启动时依赖
        litellm.request_timeout = self._timeout_seconds  # 设置请求超时

        completion_kwargs = self._build_completion_kwargs(  # 统一组装主对话调用参数
            model=model,
            messages=messages,
            temperature=temperature,
            api_key=api_key,
            tools=tools,
            enable_thinking=True,
            reasoning_effort=reasoning_effort,
        )

        # 使用 acompletion 进行流式调用
        # 注意：litellm.acompletion 在 stream=True 时返回异步迭代器，需要先 await 获取
        response_stream = await litellm.acompletion(**completion_kwargs, stream=True)
        # 立即遍历异步迭代器，每个 chunk 都会实时 yield，实现真正的流式传输
        async for chunk in response_stream:
            normalized = self._normalize_single_chunk(chunk)
            if normalized is not None:
                yield normalized

    @staticmethod
    def _load_litellm() -> Any:
        """延迟导入 LiteLLM 模块。"""
        import litellm  # 导入 litellm 库

        return litellm

    def _build_completion_kwargs(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float,
        api_key: str | None,
        tools: list[dict] | None,
        enable_thinking: bool,
        reasoning_effort: str | None,
    ) -> dict[str, Any]:
        """统一构造 LiteLLM 调用参数。

        DeepSeek V4 需要显式控制 thinking 开关。
        主对话默认开启 thinking，而上下文摘要走非流式 `complete_text()`，
        默认显式关闭 thinking，避免摘要模型额外输出思考内容。
        """
        completion_kwargs: dict[str, Any] = {  # 先写入所有模型都需要的稳定公共字段
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if api_key is not None:  # 显式提供 api_key 时才透传，便于兼容环境变量读取
            completion_kwargs["api_key"] = api_key
        if tools is not None:  # 工具定义只在主对话流式场景下会使用
            completion_kwargs["tools"] = tools

        if not self._is_deepseek_model(model):  # 非 DeepSeek 模型无需额外拼 thinking 配置
            return completion_kwargs

        # DeepSeek 官方 OpenAI 兼容入口要求把 thinking 相关参数放进请求体。
        # 这里统一通过 extra_body 透传，避免依赖 LiteLLM 不同版本的 provider 映射细节。
        extra_body: dict[str, Any] = {
            "thinking": {"type": "enabled" if enable_thinking else "disabled"},
        }
        if enable_thinking and reasoning_effort is not None:  # 只有思考模式开启时才传思考强度
            extra_body["reasoning_effort"] = reasoning_effort
        completion_kwargs["extra_body"] = extra_body
        return completion_kwargs

    @staticmethod
    def _is_deepseek_model(model: str) -> bool:
        """判断当前模型是否走 DeepSeek OpenAI 兼容协议。"""
        return model.startswith("deepseek/")

    @staticmethod
    def _extract_completion_text(response: Any) -> str | None:
        """从非流式补全响应中提取最终文本。"""
        try:  # 优先按 LiteLLM / OpenAI 兼容对象结构读取
            choices = getattr(response, "choices", None)  # 读取 choices 字段
            if choices:  # choices 非空时继续提取第一条响应
                first_choice = choices[0]  # 取第一条 choice，非流式摘要只关心首条结果
                message = getattr(first_choice, "message", None)  # 优先读取对象式 message 字段
                if message is not None:  # 存在 message 时优先从中取 content
                    content = getattr(message, "content", None)  # 对象式读取 content
                    if content is not None:
                        return content
                    if isinstance(message, dict):  # 字典式 message 同样兼容
                        dict_content = message.get("content")
                        if dict_content is not None:
                            return str(dict_content)
                if isinstance(first_choice, dict):  # 某些替身测试可能直接返回 dict 结构
                    dict_message = first_choice.get("message")
                    if isinstance(dict_message, dict) and dict_message.get("content") is not None:
                        return str(dict_message["content"])
        except Exception as e:  # 非流式响应提取失败时只记录调试日志，具体失败交给调用方收敛
            logger.debug("_extract_completion_text failed: %s", e)
        return None

    def _normalize_single_chunk(self, chunk: Any) -> LLMChunk | None:
        """将单个 LiteLLM chunk 归一化为 LLMChunk 格式。

        Args:
            chunk: LiteLLM 返回的单个 chunk 对象

        Returns:
            归一化后的 LLMChunk 对象，如果没有有效数据则返回 None
        """
        # 提取 content
        content = self._extract_content(chunk)  # 提取文本内容
        # 提取 thinking（思考内容）
        thinking = self._extract_thinking(chunk)  # 提取思考内容（兼容 DeepSeek/OpenAI 推理模型）
        # 提取 tool_calls
        tool_calls = self._extract_tool_calls(chunk)  # 提取工具调用增量
        # 提取 finish_reason
        finish_reason = self._extract_finish_reason(chunk)  # 提取完成原因
        # 提取 usage
        usage = self._extract_usage(chunk)  # 提取用量信息

        # 只有当有内容、思考内容、工具调用、完成原因或用量信息时才生成 chunk
        if content is not None or thinking is not None or tool_calls is not None or finish_reason is not None or usage is not None:  # 如果有任何有效数据
            return LLMChunk(  # 返回 LLMChunk
                content=content,  # 文本内容
                thinking=thinking,  # 思考内容
                tool_calls=tool_calls,  # 工具调用增量列表
                finish_reason=finish_reason,  # 完成原因
                usage=usage,  # 用量信息
            )
        return None


    def _extract_content(self, chunk: Any) -> str | None:  # 提取文本内容
        """从 LiteLLM chunk 中提取 content。

        Args:
            chunk: LiteLLM 返回的 chunk 对象

        Returns:
            提取的 content 字符串，如果不存在则返回 None
        """
        try:  # 尝试提取 content
            # LiteLLM chunk 结构: chunk.choices[0].delta.content
            if hasattr(chunk, "choices"):  # 如果有 choices 属性
                choices = chunk.choices  # 获取 choices
                if choices and len(choices) > 0:  # 如果 choices 不为空
                    choice = choices[0]  # 获取第一个 choice
                    if hasattr(choice, "delta"):  # 如果有 delta 属性
                        delta = choice.delta  # 获取 delta
                        if hasattr(delta, "content"):  # 如果有 content 属性
                            return delta.content  # 返回 content
            return None  # 无法提取 content
        except Exception as e:  # 捕获所有异常
            logger.debug("_extract_content failed: %s", e)  # 调试日志，用于开发/测试阶段捕捉异常
            return None  # 返回 None

    def _extract_thinking(self, chunk: Any) -> str | None:  # 提取思考内容
        """从 LiteLLM chunk 中提取思考内容（推理模型的思考过程）。

        兼容多种协议：
        - DeepSeek: choices[0].delta.reasoning_content
        - OpenAI o1/o3: choices[0].delta.reasoning_content
        - LiteLLM 统一格式: choices[0].delta.thinking

        Args:
            chunk: LiteLLM 返回的 chunk 对象

        Returns:
            提取的思考内容字符串，如果不存在则返回 None
        """
        try:  # 尝试提取思考内容
            # LiteLLM chunk 结构: chunk.choices[0].delta.{thinking|reasoning_content}
            if hasattr(chunk, "choices"):  # 如果有 choices 属性
                choices = chunk.choices  # 获取 choices
                if choices and len(choices) > 0:  # 如果 choices 不为空
                    choice = choices[0]  # 获取第一个 choice
                    if hasattr(choice, "delta"):  # 如果有 delta 属性
                        delta = choice.delta  # 获取 delta
                        
                        # 按优先级尝试提取思考内容字段
                        # 1. 尝试 DeepSeek/OpenAI 原始字段名 reasoning_content
                        if hasattr(delta, "reasoning_content"):  # 如果有 reasoning_content 属性
                            reasoning = delta.reasoning_content  # 获取 reasoning_content
                            if reasoning is not None:  # 如果 reasoning_content 不为空
                                return reasoning  # 返回 reasoning_content

                        
                        # 2. 尝试 LiteLLM 常见统一字段名 reasoning
                        if hasattr(delta, "reasoning"):  # 如果有 reasoning 属性
                            reasoning = delta.reasoning  # 获取 reasoning
                            if reasoning is not None:  # 如果 reasoning 不为空
                                return reasoning  # 返回 reasoning

                        # 3. 再兜底 LiteLLM 统一字段名 thinking
                        if hasattr(delta, "thinking"):  # 如果有 thinking 属性
                            thinking = delta.thinking  # 获取 thinking
                            if thinking is not None:  # 如果 thinking 不为空
                                return thinking  # 返回 thinking

            return None  # 无法提取思考内容
        except Exception as e:  # 捕获所有异常
            logger.debug("_extract_thinking failed: %s", e)  # 调试日志，用于开发/测试阶段捕捉异常
            return None  # 返回 None

    def _extract_tool_calls(self, chunk: Any) -> list[dict] | None:  # 提取工具调用
        """从 LiteLLM chunk 中提取 tool_calls 增量片段。

        Args:
            chunk: LiteLLM 返回的 chunk 对象

        Returns:
            工具调用增量字典列表，每个字典包含 index、id、function_name、arguments 字段
        """
        try:  # 尝试提取 tool_calls
            # LiteLLM chunk 结构: chunk.choices[0].delta.tool_calls
            if not hasattr(chunk, "choices"):  # 如果没有 choices 属性
                return None  # 返回 None
            choices = chunk.choices  # 获取 choices
            if not choices or len(choices) == 0:  # 如果 choices 为空
                return None  # 返回 None
            choice = choices[0]  # 获取第一个 choice
            if not hasattr(choice, "delta"):  # 如果没有 delta 属性
                return None  # 返回 None
            delta = choice.delta  # 获取 delta
            if not hasattr(delta, "tool_calls"):  # 如果没有 tool_calls 属性
                return None  # 返回 None
            tool_calls = delta.tool_calls  # 获取 tool_calls
            if not tool_calls:  # 如果 tool_calls 为空
                return None  # 返回 None

            # 解析每个 tool_call 增量
            result: list[dict] = []  # 初始化结果列表
            for tc in tool_calls:  # 遍历每个 tool_call
                # 提取 tool_call 的各个字段
                index = getattr(tc, "index", 0)  # 获取索引，默认为 0
                tc_id = getattr(tc, "id", None)  # 获取 ID
                function_name = None  # 初始化函数名
                arguments = None  # 初始化参数

                # 提取 function 相关信息
                if hasattr(tc, "function"):  # 如果有 function 属性
                    func = tc.function  # 获取 function
                    function_name = getattr(func, "name", None)  # 获取函数名
                    arguments = getattr(func, "arguments", None)  # 获取参数

                # 创建工具调用增量字典
                tool_call_delta: dict = {  # 创建工具调用增量字典
                    "index": index,  # 索引
                }
                if tc_id is not None:  # 如果 ID 不为空
                    tool_call_delta["id"] = tc_id  # 添加 ID 字段
                if function_name is not None:  # 如果函数名不为空
                    tool_call_delta["function_name"] = function_name  # 添加函数名字段
                if arguments is not None:  # 如果参数不为空
                    tool_call_delta["arguments"] = arguments  # 添加参数字段

                result.append(tool_call_delta)  # 添加到结果列表

            return result if result else None  # 返回结果列表或 None
        except Exception as e:  # 捕获所有异常
            logger.debug("_extract_tool_calls failed: %s", e)  # 调试日志，用于开发/测试阶段捕捉异常
            return None  # 返回 None

    def _extract_finish_reason(self, chunk: Any) -> str | None:  # 提取完成原因
        """从 LiteLLM chunk 中提取 finish_reason。

        Args:
            chunk: LiteLLM 返回的 chunk 对象

        Returns:
            提取的 finish_reason 字符串，如 "stop"、"tool_calls" 等，如果不存在则返回 None
        """
        try:  # 尝试提取 finish_reason
            # LiteLLM chunk 结构: chunk.choices[0].finish_reason
            if hasattr(chunk, "choices"):  # 如果有 choices 属性
                choices = chunk.choices  # 获取 choices
                if choices and len(choices) > 0:  # 如果 choices 不为空
                    choice = choices[0]  # 获取第一个 choice
                    if hasattr(choice, "finish_reason"):  # 如果有 finish_reason 属性
                        finish_reason = choice.finish_reason  # 获取 finish_reason
                        # 只在有实际值时返回
                        if finish_reason is not None:  # 如果完成原因不为空
                            return finish_reason  # 返回完成原因
            return None  # 无法提取 finish_reason
        except Exception as e:  # 捕获所有异常
            logger.debug("_extract_finish_reason failed: %s", e)  # 调试日志，用于开发/测试阶段捕捉异常
            return None  # 返回 None

    def _extract_usage(self, chunk: Any) -> UsageInfo | None:  # 提取用量信息
        """从 LiteLLM chunk 中提取 Token 用量信息。

        Args:
            chunk: LiteLLM 返回的 chunk 对象

        Returns:
            UsageInfo 对象，如果不存在则返回 None
        """
        try:  # 尝试提取 usage
            # LiteLLM chunk 结构: chunk.usage
            if not hasattr(chunk, "usage"):  # 如果没有 usage 属性
                return None  # 返回 None
            usage = chunk.usage  # 获取 usage
            if usage is None:  # 如果 usage 为空
                return None  # 返回 None

            # 提取用量字段并构造 UsageInfo
            prompt_tokens = getattr(usage, "prompt_tokens", 0)  # 获取 prompt_tokens，默认为 0
            completion_tokens = getattr(usage, "completion_tokens", 0)  # 获取 completion_tokens，默认为 0
            total_tokens = getattr(usage, "total_tokens", 0)  # 获取 total_tokens，默认为 0

            # 如果所有字段都为 0，表示没有有效的用量信息
            if prompt_tokens == 0 and completion_tokens == 0 and total_tokens == 0:
                return None  # 返回 None

            return UsageInfo(  # 返回 UsageInfo 对象
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            )
        except Exception as e:  # 捕获所有异常
            logger.debug("_extract_usage failed: %s", e)  # 调试日志，用于开发/测试阶段捕捉异常
            return None  # 返回 None
