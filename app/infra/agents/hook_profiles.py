"""注册单个 ToolHook 和 ModelHook，子代理按名称组合成管线。"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.hooks import ModelHook, ModelHookPipeline, ToolHook, ToolHookPipeline
from app.core.models.error import ErrorCode


@dataclass(frozen=True, slots=True)
class HookRegistry:
    """注册单个 ToolHook 和 ModelHook。

    子代理通过 tool_hook_profiles / model_hook_profiles 字段
    指定多个 hook 名称，profile builder 按顺序组装为对应管线。
    设为 None 或空元组表示不使用对应类型的 Hook。
    """

    tool_hooks: dict[str, ToolHook]
    model_hooks: dict[str, ModelHook]

    def get_tool_hook(self, name: str) -> ToolHook:
        """按名称获取 ToolHook 实例。"""
        try:
            return self.tool_hooks[name]
        except KeyError as exc:
            raise ValueError(f"{ErrorCode.INVALID_SUBAGENT_CONFIG.value}: 未知 tool_hook: {name}") from exc

    def get_model_hook(self, name: str) -> ModelHook:
        """按名称获取 ModelHook 实例。"""
        try:
            return self.model_hooks[name]
        except KeyError as exc:
            raise ValueError(f"{ErrorCode.INVALID_SUBAGENT_CONFIG.value}: 未知 model_hook: {name}") from exc
