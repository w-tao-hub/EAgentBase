"""子代理 Hook profile 注册表。"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.hooks import ToolHookPipeline
from app.core.models.error import ErrorCode


@dataclass(frozen=True, slots=True)
class HookProfileRegistry:
    """代码侧预注册 Hook 组，md 只能按名称引用。

    profile 不存在时视为配置错误，不允许隐式回退到默认 Hook。
    若子代理不需要 Hook，直接在子代理定义中将 hook_profile 设为 None。
    """

    profiles: dict[str, ToolHookPipeline]

    def get(self, name: str) -> ToolHookPipeline:
        """按名称获取 Hook 管线。"""
        try:
            return self.profiles[name]
        except KeyError as exc:
            raise ValueError(f"{ErrorCode.INVALID_SUBAGENT_CONFIG.value}: 未知 hook_profile: {name}") from exc
