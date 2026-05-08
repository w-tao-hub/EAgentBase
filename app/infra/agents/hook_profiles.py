"""子代理 Hook profile 注册表。

代码侧预注册 Hook 管线组，md 配置只能按名称引用，
避免在用户可编辑的 md 中直接配置 Hook 行为。
"""

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

    profiles: dict[str, ToolHookPipeline]  # 按名称索引的 Hook 管线映射

    def get(self, name: str) -> ToolHookPipeline:
        """按名称获取 Hook 管线，不存在时视为配置错误。

        Args:
            name: Hook profile 名称

        Returns:
            对应的 ToolHookPipeline 实例

        Raises:
            ValueError: 指定名称的 profile 不存在，错误码为 INVALID_SUBAGENT_CONFIG
        """
        try:
            return self.profiles[name]  # 从字典中查找 profile
        except KeyError as exc:  # 名称不存在时抛出明确错误
            raise ValueError(f"{ErrorCode.INVALID_SUBAGENT_CONFIG.value}: 未知 hook_profile: {name}") from exc
