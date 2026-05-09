"""Hook 相关异常定义。"""

from __future__ import annotations


class HookExecutionError(RuntimeError):
    """Hook 执行失败异常。

    用于把 fail-closed Hook 的异常包装成稳定错误类型，
    便于 Runtime / Loop 在不同阶段做统一错误收敛。
    """

    def __init__(self, stage: str, hook_name: str, original_exception: Exception) -> None:
        """初始化 HookExecutionError。

        Args:
            stage: 当前失败所处的 Hook 阶段，例如 before_model
            hook_name: 抛出异常的 Hook 类型名
            original_exception: 原始异常对象
        """
        self.stage = stage
        self.hook_name = hook_name
        self.original_exception = original_exception
        super().__init__(f"Hook 执行失败[{stage}:{hook_name}]: {original_exception}")
