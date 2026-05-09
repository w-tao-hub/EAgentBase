"""AgentLoop 实现。

提供多轮循环编排能力，支持工具调用、错误处理和最大轮数限制。
该循环器为无状态设计，所有运行依赖通过 AgentExecutionProfile 注入。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator, TYPE_CHECKING

from app.core.models.event import (
    Event,
    RunStartedEvent,
    MessageDeltaEvent,
    AssistantWithToolsEvent,
    ToolUseStartedEvent,
    ToolUseCompletedEvent,
    RunCompletedEvent,
    RunFailedEvent,
    RunCancelledEvent,
)
from app.core.models.error import ErrorCode
from app.core.models.execution_context import ExecutionContext
from app.core.models.stored_message import StoredMessage
from app.core.hooks import (
    HookExecutionError,
    ToolRequest,
    ToolResponse,
)
from app.core.models.tool import (
    ToolResult,
    ToolExecuteItem,
    ToolExecuteResult,
)
from app.core.runtime.agent_runtime import TurnComplete, ToolCall

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.core.models.agent import AgentExecutionProfile


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
        self._default_max_turns = default_max_turns

    async def run(
        self,
        *,
        run_id: str,
        profile: "AgentExecutionProfile",
        messages: list[dict],
        session_id: str = "",
        context: ExecutionContext | None = None,
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
        try:
            agent = profile.agent
            max_turns = profile.max_turns or self._default_max_turns
            tool_registry = profile.tool_registry
            runtime = profile.runtime
            tool_hook_pipeline = profile.tool_hook_pipeline

            execution_context = context or ExecutionContext(
                run_id=run_id,
                session_id=session_id,
                metadata=None,
                agent=agent,
            )

            # Step 1: 发出 run_started 事件
            yield RunStartedEvent(
                run_id=run_id,
                session_id=session_id,
            )

            conversation_messages = list(messages)

            turn_count = 0

            while True:
                turn_count += 1

                # 检查是否超过最大轮数限制
                if turn_count > max_turns:
                    logger.warning("超过最大轮数限制: run_id=%s, max_turns=%d", run_id, max_turns)
                    yield RunFailedEvent(
                        run_id=run_id,
                        error_code=ErrorCode.MAX_TURNS_EXCEEDED,
                        message=f"超过最大轮数限制 (max_turns={max_turns})",
                    )
                    return

                logger.debug("开始第 %d 轮对话", turn_count)

                if context is not None and context.cancel_event.is_set():
                    raise asyncio.CancelledError("Run cancelled before LLM call")

                text_parts: list[str] = []
                tool_calls: list[ToolCall] = []
                reasoning_content: str | None = None

                try:
                    tools = tool_registry.to_llm_tools()
                    async for chunk in runtime.stream_once(
                        agent=agent,
                        messages=conversation_messages,
                        tools=tools,
                        context=execution_context,
                    ):
                        if isinstance(chunk, str):
                            text_parts.append(chunk)
                            yield MessageDeltaEvent(
                                run_id=run_id,
                                content=chunk,
                            )

                        elif isinstance(chunk, TurnComplete):
                            if chunk.tool_calls:
                                tool_calls = chunk.tool_calls
                            reasoning_content = chunk.reasoning_content

                except HookExecutionError as e:
                    logger.error("Hook 调用失败: run_id=%s, error=%s", run_id, e)
                    yield RunFailedEvent(
                        run_id=run_id,
                        error_code=ErrorCode.HOOK_EXECUTION_FAILED,
                        message=str(e),
                    )
                    return

                except Exception as e:
                    logger.error("LLM 调用失败: run_id=%s, error=%s", run_id, e)
                    yield RunFailedEvent(
                        run_id=run_id,
                        error_code=ErrorCode.LLM_REQUEST_FAILED,
                        message=f"LLM 调用失败: {str(e)}",
                    )
                    return

                # # 检查是否需要处理工具调用或正常结束
                if not tool_calls:  # # 如果没有工具调用，正常结束

                    # 将助手消息加入历史（便于后续可能的继续对话）。预留设计：中间轮次出现纯文本响应（无工具调用）是不可能的
                    # assistant_message = {
                    #     "role": "assistant",
                    #     "content": "".join(text_parts) if text_parts else None,
                    #     }
                    # conversation_messages.append(assistant_message)


                    full_output = "".join(text_parts)
                    yield RunCompletedEvent(
                        run_id=run_id,
                        output=full_output,
                        reasoning_content=reasoning_content,
                    )
                    return

                else:
                    assistant_message: dict = {
                        "role": "assistant",
                        "content": "".join(text_parts) if text_parts else None,
                    }
                    if reasoning_content is not None:
                        assistant_message["reasoning_content"] = reasoning_content

                    tool_calls_list = [
                        {
                            "id": tc.id,
                            "type": tc.type,
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in tool_calls
                    ]
                    assistant_message["tool_calls"] = tool_calls_list

                    # 先发出 AssistantWithToolsEvent，让 ChatService 存储助手消息
                    yield AssistantWithToolsEvent(
                        run_id=run_id,
                        content=assistant_message["content"],
                        reasoning_content=reasoning_content,
                        tool_calls=tool_calls_list,
                    )

                    conversation_messages.append(assistant_message)

                    # 步骤1: 顺序预处理，发出 started 事件，收集可执行工具
                    tools_to_execute: list[ToolExecuteItem] = []

                    for tc in tool_calls:
                        tool_name = tc.function.name
                        tool_call_id = tc.id
                        arguments_str = tc.function.arguments

                        try:
                            tool_input = json.loads(arguments_str) if arguments_str else {}
                        except json.JSONDecodeError as e:
                            logger.warning("工具参数 JSON 解析失败: %s", e)
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
                            conversation_messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "content": f"工具参数 JSON 解析失败: {str(e)}",
                            })
                            continue

                        tool = tool_registry.get(tool_name)

                        if tool is None:
                            logger.warning("未知工具: %s", tool_name)
                            # 未知工具无法进入 before_tool Hook，但仍保持既有 started -> completed 事件序列。
                            yield ToolUseStartedEvent(
                                run_id=run_id,
                                tool_call_id=tool_call_id,
                                tool_name=tool_name,
                                tool_input=tool_input,
                            )
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
                        else:
                            tool_request = ToolRequest(
                                tool_name=tool_name,
                                tool_call_id=tool_call_id,
                                tool_input=tool_input,
                                tool=tool,
                            )
                            try:
                                tool_request = await tool_hook_pipeline.before_tool(tool_request, execution_context)
                            except HookExecutionError as hook_error:
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
                                continue

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

                    # 步骤2: 并行执行所有正常工具
                    if tools_to_execute:
                        # 在并行执行前再次检查取消信号
                        if context is not None and context.cancel_event.is_set():
                            raise asyncio.CancelledError("Run cancelled before tool execution")

                        async def _execute_tool(item: ToolExecuteItem) -> ToolExecuteResult:
                            """执行单个工具，返回 ToolExecuteResult 数据类。"""
                            tool_context = execution_context.for_tool_call(
                                tool_call_id=item.tool_call_id,
                                tool_name=item.tool_name,
                            )
                            try:
                                result = await item.tool.call(item.tool_input, tool_context)
                            except Exception as e:
                                logger.error("工具执行失败: %s, error=%s", item.tool_name, e)
                                result = ToolResult(content=f"工具执行失败: {str(e)}", is_error=True)

                            tool_response = ToolResponse(
                                tool_name=item.tool_name,
                                tool_call_id=item.tool_call_id,
                                result=result,
                            )
                            try:
                                tool_response = await tool_hook_pipeline.after_tool(tool_response, execution_context)
                                return ToolExecuteResult(
                                    tool_call_id=tool_response.tool_call_id,
                                    tool_name=tool_response.tool_name,
                                    result=tool_response.result,
                                )
                            except HookExecutionError as hook_error:
                                logger.error("工具 after_hook 执行失败: tool=%s, error=%s", item.tool_name, hook_error)
                                return ToolExecuteResult(
                                    tool_call_id=item.tool_call_id,
                                    tool_name=item.tool_name,
                                    result=ToolResult(content=str(hook_error), is_error=True),
                                )

                        tool_tasks = [
                            asyncio.create_task(_execute_tool(item))
                            for item in tools_to_execute
                        ]

                        executed_results = await asyncio.gather(*tool_tasks, return_exceptions=True)

                        # 步骤3: 按原顺序发出 ToolUseCompletedEvent
                        for i, item in enumerate(tools_to_execute):
                            exec_result = executed_results[i]

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

                            conversation_messages.append({
                                "role": "tool",
                                "tool_call_id": exec_result.tool_call_id,
                                "content": exec_result.result.content,
                            })
                            # 工具若返回附带消息，则紧跟 tool 结果继续入上下文
                            if exec_result.result.stored_message is not None:
                                conversation_messages.append(
                                    self._stored_message_to_conversation_dict(exec_result.result.stored_message)
                                )

                    continue

        except asyncio.CancelledError as e:
            # 只有因外部取消信号导致的 CancelledError 才收敛为 RunCancelledEvent
            if context is not None and context.cancel_event.is_set():
                logger.warning("AgentLoop 收到取消信号: run_id=%s, error=%s", run_id, e)
                yield RunCancelledEvent(
                    run_id=run_id,
                    reason=str(e) if str(e) else "Run cancelled",
                    error_code=ErrorCode.RUN_CANCELLED,
                )
                return
            raise

        except Exception as e:
            logger.error("AgentLoop 执行异常: run_id=%s, error=%s", run_id, e, exc_info=True)
            yield RunFailedEvent(
                run_id=run_id,
                error_code=ErrorCode.LLM_REQUEST_FAILED,
                message=f"AgentLoop 执行异常: {str(e)}",
            )
            return

    @staticmethod
    def _stored_message_to_conversation_dict(message: StoredMessage) -> dict:
        """把工具附带的 StoredMessage 归一化为运行时对话消息。"""
        msg_dict: dict = {"role": message.role}
        if message.content is not None:
            msg_dict["content"] = message.content
        if message.tool_calls is not None:
            msg_dict["tool_calls"] = message.tool_calls
        if message.tool_call_id is not None:
            msg_dict["tool_call_id"] = message.tool_call_id
        if message.name is not None:
            msg_dict["name"] = message.name
        return msg_dict
