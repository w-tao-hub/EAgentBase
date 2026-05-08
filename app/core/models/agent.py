"""Agent 领域模型定义。"""

from __future__ import annotations  # 启用未来注解，避免前向引用问题

from dataclasses import dataclass, field  # 导入数据类装饰器和字段函数，用于 AgentExecutionProfile
from typing import Literal, TYPE_CHECKING  # 导入 Literal 类型和前向引用标记

from pydantic import BaseModel, Field  # 导入 Pydantic v2 的基础模型和字段工具

if TYPE_CHECKING:  # 仅在类型检查时导入，避免运行时的循环导入问题
    from app.core.models.tool import ToolRegistry  # 工具注册表类型
    from app.core.hooks.pipeline import ToolHookPipeline  # 工具 Hook 管线类型


class Agent(BaseModel):
    """表示一个 AI Agent 的静态配置实体。

    Agent 本身不保存运行状态，只保存用于与 LLM 交互的元数据。
    """

    # Agent 的唯一标识符，在系统内不可重复
    agent_id: str = Field(min_length=1)

    # Agent 的显示名称，用于前端展示或日志描述
    name: str = Field(min_length=1)

    # Agent 的能力描述，简要说明该 Agent 适合处理什么类型的任务
    description: str | None = Field(default=None)

    # 指定调用的大模型名称，例如 gpt-4.1-mini
    model: str = Field(min_length=1)

    # 系统级提示词，在每次对话开始时注入
    system_prompt: str = Field(min_length=1)

    # 采样温度，控制生成文本的随机性，范围 0.0 ~ 2.0
    temperature: float = Field(ge=0.0, le=2.0)

    # 推理模型的思考强度。
    # 当前主链路只对 DeepSeek V4 thinking 模型生效，并限制为 high / max 两档。
    reasoning_effort: str = Field(default="high", pattern=r"^(high|max)$")


@dataclass(frozen=True, slots=True)
class AgentPromptSource:
    """记录 Agent prompt 来源，便于排障和配置审计。

    本期只支持 file 类型的 prompt 来源，后续可扩展数据库、API 等来源。
    """

    # prompt 来源类型，当前仅支持文件来源
    kind: Literal["file"]

    # prompt 文件的路径，用于定位和加载 prompt 内容
    path: str


@dataclass(slots=True)
class AgentExecutionProfile:
    """表示一次 Agent 执行所需的完整运行配置。

    将 Agent 的静态元信息与运行时依赖解耦，Agent 本身只保存模型配置，
    运行时的工具注册表、Hook 管线、skills 等由本 profile 统一承载。
    """

    # Agent 的唯一标识符，用于运行追踪和日志关联
    agent_id: str

    # Agent 静态配置实体，包含模型、系统提示、温度等元信息
    agent: Agent

    # prompt 来源记录，用于配置审计和排障溯源
    prompt_source: AgentPromptSource

    # Agent 运行时实例，负责与 LLM 的交互和流式响应处理
    runtime: object

    # 工具注册表，包含当前执行可用的全部工具
    tool_registry: "ToolRegistry"

    # 工具 Hook 管线，用于工具调用前后的拦截和处理
    tool_hook_pipeline: "ToolHookPipeline"

    # 最大对话轮数，超过此限制运行将自动终止
    max_turns: int

    # 可选的技能列表，用于扩展 Agent 的能力范围
    skills: tuple[str, ...] = field(default_factory=tuple)

    # 额外的系统消息，会附加到 Agent 的 system_prompt 之后
    extra_system_messages: tuple[str, ...] = field(default_factory=tuple)
