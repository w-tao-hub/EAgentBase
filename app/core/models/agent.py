"""Agent 领域模型定义。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from app.core.models.tool import ToolRegistry
    from app.core.hooks.pipeline import ToolHookPipeline


class Agent(BaseModel):
    """表示一个 AI Agent 的静态配置实体。

    Agent 本身不保存运行状态，只保存用于与 LLM 交互的元数据。
    """

    agent_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str | None = Field(default=None)
    model: str = Field(min_length=1)
    system_prompt: str = Field(min_length=1)
    temperature: float = Field(ge=0.0, le=2.0)
    reasoning_effort: str = Field(default="high", pattern=r"^(high|max)$")


@dataclass(frozen=True, slots=True)
class AgentPromptSource:
    """记录 Agent prompt 来源，便于排障和配置审计。

    本期只支持 file 类型的 prompt 来源，后续可扩展数据库、API 等来源。
    """

    kind: Literal["file"]
    path: str


@dataclass(slots=True)
class AgentExecutionProfile:
    """表示一次 Agent 执行所需的完整运行配置。

    将 Agent 的静态元信息与运行时依赖解耦，Agent 本身只保存模型配置，
    运行时的工具注册表、Hook 管线、skills 等由本 profile 统一承载。
    """

    agent_id: str
    agent: Agent
    prompt_source: AgentPromptSource
    runtime: object
    tool_registry: "ToolRegistry"
    tool_hook_pipeline: "ToolHookPipeline"
    max_turns: int
    skills: tuple[str, ...] = field(default_factory=tuple)
    extra_system_messages: tuple[str, ...] = field(default_factory=tuple)
