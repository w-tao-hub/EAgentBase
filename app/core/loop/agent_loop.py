"""AgentLoop 实现。

提供多轮循环编排能力，支持工具调用、错误处理和最大轮数限制。
该循环器为无状态设计，所有运行依赖通过 AgentExecutionProfile 注入。
"""

from __future__ import annotations  # # 启用未来注解

import asyncio  # # 导入异步模块，用于并行执行工具
import json  # # 导入 JSON 处理模块
import logging  # # 导入标准库日志模块，避免 core 反向依赖 infra
from typing import AsyncIterator, TYPE_CHECKING  # # 导入类型提示

# # 导入事件模型
from app.core.models.event import (
    Event,  # # 事件基类
    RunStartedEvent,  # # 运行开始事件
    MessageDeltaEvent,  # # 消息增量事件
    AssistantWithToolsEvent,  # # 带工具调用的助手消息事件
    ToolUseStartedEvent,  # # 工具使用开始事件
    ToolUseCompletedEvent,  # # 工具使用完成事件
    RunCompletedEvent,  # # 运行完成事件
    RunFailedEvent,  # # 运行失败事件
    RunCancelledEvent,  # # 运行取消事件
)
from app.core.models.error import ErrorCode  # # 导入错误码枚举
from app.core.models.execution_context import ExecutionContext  # # 导入执行上下文模型
from app.core.models.stored_message import StoredMessage  # # 导入存储消息模型，便于拼接工具附带消息。
from app.core.hooks import (  # # 导入 Hook 相关抽象
    HookExecutionError,
    ToolRequest,
    ToolResponse,
)
from app.core.models.tool import (
    ToolResult,  # # 工具执行结果
    ToolExecuteItem,  # # 待执行工具项数据类
    ToolExecuteResult,  # # 工具执行结果数据类
)  # # 导入工具相关模型
from app.core.runtime.agent_runtime import TurnComplete, ToolCall  # # 导入运行时类型

# # 获取模块级日志器。
# # 直接使用标准库 logging，保持 core 层不依赖 infra 包路径。
logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # 仅在类型检查时导入
    from app.core.models.agent import AgentExecutionProfile  # Agent 执行配置类型


class AgentLoop:
    """Agent 循环编排器（无状态设计）。

    所有运行依赖通过 AgentExecutionProfile 注入，循环器本身不持有
    运行状态。负责多轮对话循环，包括：
    1. 调用 LLM 获取响应
    2. 处理文本输出（MessageDeltaEvent）
    3. 处理工具调用（ToolUseStartedEvent、ToolUseCompletedEvent）
    4. 将工具结果反馈给 LLM 继续对话
    5. 控制最大轮数，防止无限循环
    6. 所有内部失败收敛为 RunFailedEvent
    """

    def __init__(self, default_max_turns: int = 10) -> None:
        """初始化无状态循环器。

        Args:
            default_max_turns: 默认最大轮数，当 profile.max_turns 为 0 时作为兜底值
        """
        self._default_max_turns = default_max_turns  # 保存默认最大轮数

    async def run(
        self,
        *,
        run_id: str,  # 运行唯一标识
        profile: "AgentExecutionProfile",  # Agent 执行配置，携带所有运行依赖
        messages: list[dict],  # 初始消息列表
        session_id: str = "",  # 会话唯一标识
        context: ExecutionContext | None = None,  # 执行上下文，由 ChatService 构建
    ) -> AsyncIterator[Event]:
        """执行多轮循环，返回事件流。

        所有运行依赖从 profile 解构，循环器本身保持无状态。

        Args:
            run_id: 运行唯一标识
            profile: Agent 执行配置，包含 agent、runtime、tool_registry 等全部依赖
            messages: 初始消息列表，元素格式为 {"role": str, "content": str}
            session_id: 会话唯一标识
            context: 执行上下文，由 ChatService 构建并传递，包含 run_id、session_id、
                    metadata、agent 等信息，供 Tool 和未来的 Hook 使用

        Yields:
            RunStartedEvent: 循环开始时发出
            MessageDeltaEvent: LLM 返回文本片段时发出
            ToolUseStartedEvent: 工具开始执行时发出
            ToolUseCompletedEvent: 工具执行完成时发出
            RunCompletedEvent: 循环正常结束时发出
            RunFailedEvent: 循环异常结束时发出

        Note:
            该方法承诺不向外抛出任何异常。所有内部错误都会被捕获
            并收敛为 RunFailedEvent 事件。
        """
        try:  # 包裹整个执行流程，捕获所有异常
            # 从 profile 解构所有运行依赖
            agent = profile.agent  # Agent 静态配置
            max_turns = profile.max_turns or self._default_max_turns  # 最大轮数，0 时兜底
            tool_registry = profile.tool_registry  # 工具注册表
            runtime = profile.runtime  # Agent 运行时实例
            tool_hook_pipeline = profile.tool_hook_pipeline  # 工具 Hook 管线

            execution_context = context or ExecutionContext(  # 为测试与兼容场景兜底构造执行上下文
                run_id=run_id,  # 使用当前 run_id 构造上下文
                session_id=session_id,  # 使用当前 session_id 构造上下文
                metadata=None,  # 未显式传入时默认无 metadata
                agent=agent,  # 当前 Agent 始终可用
            )

            # Step 1: 发出 run_started 事件
            yield RunStartedEvent(  # 生成运行开始事件
                run_id=run_id,  # 设置 run_id
                session_id=session_id,  # 设置 session_id
            )

            # 初始化对话消息列表（复制初始消息，避免修改原始列表）
            conversation_messages = list(messages)  # 复制消息列表

            # 初始化轮数计数
            turn_count = 0  # 轮数计数器

            # 多轮循环
            while True:  # 无限循环，内部检查轮数限制
                turn_count += 1  # 增加轮数计数

                # 检查是否超过最大轮数限制
                if turn_count > max_turns:  # 如果超过最大限制
                    logger.warning("超过最大轮数限制: run_id=%s, max_turns=%d", run_id, max_turns)  # 记录警告日志
                    yield RunFailedEvent(  # 生成运行失败事件
                        run_id=run_id,  # 设置 run_id
                        error_code=ErrorCode.MAX_TURNS_EXCEEDED,  # 设置错误码为最大轮数超限
                        message=f"超过最大轮数限制 (max_turns={max_turns})",  # 设置错误消息
                    )
                    return  # 结束流

                logger.debug("开始第 %d 轮对话", turn_count)  # 记录调试日志

                # # 在进入 LLM 调用前检查取消信号，若已设置则直接抛出 CancelledError
                if context is not None and context.cancel_event.is_set():
                    raise asyncio.CancelledError("Run cancelled before LLM call")

                # # 初始化响应收集变量
                text_parts: list[str] = []  # # 文本片段列表
                tool_calls: list[ToolCall] = []  # # 工具调用列表
                reasoning_content: str | None = None  # # 当前轮累计的 reasoning_content，供工具续跑和最终持久化使用

                try:  # 尝试调用 LLM
                    # 获取可用的工具列表
                    tools = tool_registry.to_llm_tools()  # 转换为 LLM 工具格式
                    async for chunk in runtime.stream_once(  # 遍历 LLM 返回的 chunks
                        agent=agent,  # 传递 agent 配置
                        messages=conversation_messages,  # 传递当前对话消息
                        tools=tools,  # 传递工具列表
                        context=execution_context,  # 透传执行上下文，供模型 Hook 与守卫使用
                    ):
                        # # 处理 str 类型（流式文本片段）
                        if isinstance(chunk, str):  # # 如果是文本字符串
                            text_parts.append(chunk)  # # 添加到文本片段列表
                            yield MessageDeltaEvent(  # # 立即生成消息增量事件（真正流式）
                                run_id=run_id,  # # 设置 run_id
                                content=chunk,  # # 设置 content
                            )

                        # # 处理 TurnComplete 类型（单次调用完成标记）
                        elif isinstance(chunk, TurnComplete):  # # 如果是完成标记
                            # # TurnComplete 不包含文本，文本已在 str 中 yield
                            # # 这里只收集 tool_calls
                            if chunk.tool_calls:  # # 如果有工具调用
                                tool_calls = chunk.tool_calls  # # 保存工具调用列表
                            reasoning_content = chunk.reasoning_content  # # 无论是否有工具调用，都保留本轮 thinking 聚合结果

                except HookExecutionError as e:  # 捕获 Hook 执行异常
                    logger.error("Hook 调用失败: run_id=%s, error=%s", run_id, e)  # 记录 Hook 错误日志
                    yield RunFailedEvent(  # 生成运行失败事件
                        run_id=run_id,  # 设置 run_id
                        error_code=ErrorCode.HOOK_EXECUTION_FAILED,  # 设置错误码为 Hook 执行失败
                        message=str(e),  # 直接透传稳定错误消息
                    )
                    return  # 结束流

                except Exception as e:  # 捕获 LLM 调用异常
                    logger.error("LLM 调用失败: run_id=%s, error=%s", run_id, e)  # 记录错误日志
                    yield RunFailedEvent(  # 生成运行失败事件
                        run_id=run_id,  # 设置 run_id
                        error_code=ErrorCode.LLM_REQUEST_FAILED,  # 设置错误码
                        message=f"LLM 调用失败: {str(e)}",  # 设置错误消息
                    )
                    return  # 结束流

                # # 检查是否需要处理工具调用或正常结束
                if not tool_calls:  # # 如果没有工具调用，正常结束

                    # 将助手消息加入历史（便于后续可能的继续对话）。预留设计：中间轮次出现纯文本响应（无工具调用）是不可能的
                    # assistant_message = {
                    #     "role": "assistant",
                    #     "content": "".join(text_parts) if text_parts else None,
                    #     }
                    # conversation_messages.append(assistant_message)


                    # # 正常结束，发出 run_completed 事件
                    full_output = "".join(text_parts)  # # 拼接完整输出
                    yield RunCompletedEvent(  # # 生成运行完成事件
                        run_id=run_id,  # # 设置 run_id
                        output=full_output,  # # 设置最终输出
                        reasoning_content=reasoning_content,  # # 把 thinking 聚合结果交给服务层持久化
                    )
                    return  # # 正常结束

                else:  # # 有工具调用，处理工具调用
                    # # 构造助手消息（包含 tool_calls）
                    assistant_message: dict = {  # # 构造助手消息
                        "role": "assistant",  # # 角色为 assistant
                        "content": "".join(text_parts) if text_parts else None,  # # 文本内容
                    }
                    if reasoning_content is not None:  # # DeepSeek 官方要求后续轮次继续回传带工具调用 assistant 的 reasoning_content
                        assistant_message["reasoning_content"] = reasoning_content  # # 仅在真实存在 reasoning 内容时才写入消息结构

                    # # 添加 tool_calls 字段（OpenAI 格式）
                    tool_calls_list = [  # # 构造 tool_calls 列表
                        {
                            "id": tc.id,  # # 工具调用 id
                            "type": tc.type,  # # 类型（通常为 function）
                            "function": {  # # 函数信息
                                "name": tc.function.name,  # # 函数名
                                "arguments": tc.function.arguments,  # # 参数
                            },
                        }
                        for tc in tool_calls  # # 遍历所有工具调用
                    ]
                    assistant_message["tool_calls"] = tool_calls_list

                    # # 【新增】先发出 AssistantWithToolsEvent，让 ChatService 存储助手消息
                    yield AssistantWithToolsEvent(
                        run_id=run_id,
                        content=assistant_message["content"],
                        reasoning_content=reasoning_content,
                        tool_calls=tool_calls_list,
                    )

                    # 添加助手消息到对话
                    conversation_messages.append(assistant_message)  # 添加助手消息

                    # # 【并行执行】处理工具调用S
                    # 步骤1: 顺序预处理，发出 started 事件，收集可执行工具
                    tools_to_execute: list[ToolExecuteItem] = []  # 待执行工具列表

                    for tc in tool_calls:  # # 遍历所有工具调用
                        tool_name = tc.function.name  # # 获取工具名称
                        tool_call_id = tc.id  # # 获取工具调用 id
                        arguments_str = tc.function.arguments  # # 获取参数字符串

                        # 解析参数
                        try:  # 尝试解析 JSON 参数
                            tool_input = json.loads(arguments_str) if arguments_str else {}  # 解析参数
                        except json.JSONDecodeError as e:  # 捕获 JSON 解析错误
                            logger.warning("工具参数 JSON 解析失败: %s", e)  # 记录警告日志
                            # 发出 ToolUseStartedEvent
                            yield ToolUseStartedEvent(
                                run_id=run_id,
                                tool_call_id=tool_call_id,
                                tool_name=tool_name,
                                tool_input={},
                            )
                            yield ToolUseCompletedEvent(
                                run_id=run_id,
                                tool_call_id=tool_call_id,
                                tool_name=tool_name,
                                result=f"工具参数 JSON 解析失败: {str(e)}",
                                is_error=True,
                                stored_message=None,
                                task_child_id=None,
                            )
                            # 添加工具结果消息到对话
                            conversation_messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "content": f"工具参数 JSON 解析失败: {str(e)}",
                            })
                            continue  # # 继续处理下一个工具调用

                        # 查找工具
                        tool = tool_registry.get(tool_name)  # # 从注册表查找工具

                        if tool is None:  # # 如果工具未找到
                            logger.warning("未知工具: %s", tool_name)  # 记录警告日志
                            # 未知工具无法进入 before_tool Hook，但仍保持既有 started -> completed 事件序列。
                            yield ToolUseStartedEvent(
                                run_id=run_id,
                                tool_call_id=tool_call_id,
                                tool_name=tool_name,
                                tool_input=tool_input,
                            )
                            #发出 ToolUseCompletedEvent
                            yield ToolUseCompletedEvent(
                                run_id=run_id,
                                tool_call_id=tool_call_id,
                                tool_name=tool_name,
                                result=f"未知工具: {tool_name}",
                                is_error=True,
                                stored_message=None,
                                task_child_id=None,
                            )
                            # 添加工具结果消息到对话
                            conversation_messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "content": f"未知工具: {tool_name}",
                            })
                        else:  # # 工具找到，加入待执行列表
                            tool_request = ToolRequest(  # 构造工具 Hook 请求对象
                                tool_name=tool_name,  # 当前工具名称
                                tool_call_id=tool_call_id,  # 当前工具调用 ID
                                tool_input=tool_input,  # 当前解析后的工具参数
                                tool=tool,  # 当前要执行的工具实例
                            )
                            try:  # 先执行 before_tool Hook，确保 started 事件使用改写后的参数
                                tool_request = await tool_hook_pipeline.before_tool(tool_request, execution_context)
                            except HookExecutionError as hook_error:  # fail-closed Hook 失败时只收敛当前工具调用
                                logger.error("工具 before_hook 执行失败: tool=%s, error=%s", tool_name, hook_error)
                                yield ToolUseCompletedEvent(
                                    run_id=run_id,
                                    tool_call_id=tool_call_id,
                                    tool_name=tool_name,
                                    result=str(hook_error),
                                    is_error=True,
                                    task_child_id=None,
                                )
                                conversation_messages.append({
                                    "role": "tool",
                                    "tool_call_id": tool_call_id,
                                    "content": str(hook_error),
                                })
                                continue  # 当前工具调用结束，继续处理下一个工具

                            # 发出 ToolUseStartedEvent，确保事件与真实执行参数一致
                            yield ToolUseStartedEvent(
                                run_id=run_id,
                                tool_call_id=tool_request.tool_call_id,
                                tool_name=tool_request.tool_name,
                                tool_input=tool_request.tool_input,
                            )

                            tools_to_execute.append(ToolExecuteItem(
                                tool_call_id=tool_request.tool_call_id,
                                tool_name=tool_request.tool_name,
                                tool_input=tool_request.tool_input,
                                tool=tool_request.tool,
                            ))

                    # # 【并行执行】步骤2: 并行执行所有正常工具
                    if tools_to_execute:  # # 如果有需要执行的工具
                        # # 在并行执行前再次检查取消信号
                        if context is not None and context.cancel_event.is_set():
                            raise asyncio.CancelledError("Run cancelled before tool execution")

                        async def _execute_tool(item: ToolExecuteItem) -> ToolExecuteResult:
                            """执行单个工具，返回 ToolExecuteResult 数据类。"""
                            # 为当前工具调用派生执行上下文，让工具能准确获知自己的调用 ID 和名称
                            tool_context = execution_context.for_tool_call(
                                tool_call_id=item.tool_call_id,
                                tool_name=item.tool_name,
                            )
                            try:  # # 尝试执行工具
                                result = await item.tool.call(item.tool_input, tool_context)  # # 使用派生的工具上下文
                            except Exception as e:  # # 捕获工具执行异常并收敛为错误 ToolResult
                                logger.error("工具执行失败: %s, error=%s", item.tool_name, e)
                                result = ToolResult(content=f"工具执行失败: {str(e)}", is_error=True)

                            tool_response = ToolResponse(  # # 构造 after_tool Hook 响应对象
                                tool_name=item.tool_name,
                                tool_call_id=item.tool_call_id,
                                result=result,
                            )
                            try:  # # 尝试执行 after_tool Hook，允许改写最终结果
                                tool_response = await tool_hook_pipeline.after_tool(tool_response, execution_context)
                                return ToolExecuteResult(
                                    tool_call_id=tool_response.tool_call_id,
                                    tool_name=tool_response.tool_name,
                                    result=tool_response.result,
                                )
                            except HookExecutionError as hook_error:  # # fail-closed after_tool 失败时，只收敛当前工具为错误结果
                                logger.error("工具 after_hook 执行失败: tool=%s, error=%s", item.tool_name, hook_error)
                                return ToolExecuteResult(
                                    tool_call_id=item.tool_call_id,
                                    tool_name=item.tool_name,
                                    result=ToolResult(content=str(hook_error), is_error=True),
                                )

                        # 创建所有执行任务
                        tool_tasks = [
                            asyncio.create_task(_execute_tool(item))
                            for item in tools_to_execute
                        ]

                        # 等待所有工具执行完成
                        executed_results = await asyncio.gather(*tool_tasks, return_exceptions=True)

                        # # 【并行执行】步骤3: 按原顺序发出 ToolUseCompletedEvent
                        for i, item in enumerate(tools_to_execute):
                            exec_result = executed_results[i]

                            # 处理未捕获的异常（理论上不应发生）
                            if isinstance(exec_result, Exception):
                                logger.error("工具执行出现未捕获异常: %s, error=%s", item.tool_name, exec_result)
                                exec_result = ToolExecuteResult(
                                    tool_call_id=item.tool_call_id,
                                    tool_name=item.tool_name,
                                    result=ToolResult(
                                        content=f"工具执行失败: {str(exec_result)}",
                                        is_error=True,
                                    ),
                                )

                            # 发出 ToolUseCompletedEvent
                            yield ToolUseCompletedEvent(
                                run_id=run_id,
                                tool_call_id=exec_result.tool_call_id,
                                tool_name=exec_result.tool_name,
                                result=exec_result.result.content,
                                is_error=exec_result.result.is_error,
                                stored_message=exec_result.result.stored_message,
                                task_child_id=(
                                    exec_result.result.meta.task_child_id
                                    if exec_result.result.meta is not None
                                    else None
                                ),
                            )

                            # 添加工具结果消息到对话
                            conversation_messages.append({
                                "role": "tool",
                                "tool_call_id": exec_result.tool_call_id,
                                "content": exec_result.result.content,
                            })
                            if exec_result.result.stored_message is not None:  # 工具若返回附带消息，则紧跟 tool 结果继续入上下文。
                                conversation_messages.append(
                                    self._stored_message_to_conversation_dict(exec_result.result.stored_message)
                                )

                    # # 继续下一轮对话
                    continue  # # 继续循环

        except asyncio.CancelledError as e:  # 捕获取消异常
            # 只有因外部取消信号导致的 CancelledError 才收敛为 RunCancelledEvent；
            # 若 cancel_event 未被设置（如锁心跳丢失直接 cancel 主任务），
            # 则继续向上抛出，由 ChatRunLockScope 转换为受控业务异常。
            if context is not None and context.cancel_event.is_set():
                logger.warning("AgentLoop 收到取消信号: run_id=%s, error=%s", run_id, e)
                yield RunCancelledEvent(
                    run_id=run_id,
                    reason=str(e) if str(e) else "Run cancelled",
                    error_code=ErrorCode.RUN_CANCELLED,
                )
                return  # 结束流
            raise

        except Exception as e:  # 捕获所有未处理的异常
            # 任何其他未处理的异常都收敛为 run_failed 事件
            logger.error("AgentLoop 执行异常: run_id=%s, error=%s", run_id, e, exc_info=True)  # 记录错误日志
            yield RunFailedEvent(  # 生成运行失败事件
                run_id=run_id,  # 设置 run_id
                error_code=ErrorCode.LLM_REQUEST_FAILED,  # 设置错误码
                message=f"AgentLoop 执行异常: {str(e)}",  # 设置错误消息
            )
            return  # 结束流

    @staticmethod
    def _stored_message_to_conversation_dict(message: StoredMessage) -> dict:
        """把工具附带的 StoredMessage 归一化为运行时对话消息。"""
        msg_dict: dict = {"role": message.role}  # # role 字段始终必传，确保运行时消息结构合法。
        if message.content is not None:  # # content 仅在存在时输出，避免制造无意义空字段。
            msg_dict["content"] = message.content
        if message.tool_calls is not None:  # # assistant 工具请求消息需要保留 tool_calls。
            msg_dict["tool_calls"] = message.tool_calls
        if message.tool_call_id is not None:  # # tool 结果消息需要保留 tool_call_id 以维持配对关系。
            msg_dict["tool_call_id"] = message.tool_call_id
        if message.name is not None:  # # 具名 tool 消息需要保留 name，便于下游识别来源。
            msg_dict["name"] = message.name
        return msg_dict
