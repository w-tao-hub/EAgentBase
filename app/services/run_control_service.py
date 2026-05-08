"""RunControlService 实现。

提供 Run 查询能力。
"""

from __future__ import annotations  # 启用未来注解

import logging  # 导入标准库日志模块，避免 services 依赖 infra 包路径
from typing import TYPE_CHECKING  # 导入类型检查标记

from app.core.models.run import Run  # 导入 Run 模型
from app.core.models.error import AppError, ErrorCode  # 导入错误模型和枚举

# 获取模块级日志器。
# 直接使用标准库 logging，保持 services 层不依赖 infra 包路径。
logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # 仅在类型检查时导入
    from app.infra.store.redis_run_store import RedisRunStore  # Run 存储类型


class RunControlService:
    """Run 控制服务。

    负责 Run 的查询和管理，包括：
    1. 根据 run_id 查询 Run 详情
    2. 返回类型化的结果（Run 或 AppError）
    """

    def __init__(  # 构造函数
        self,
        run_store: RedisRunStore,  # Run 存储实例
        chat_service: object,  # ChatService 实例，用于代理取消请求
    ) -> None:
        """初始化 RunControlService。

        Args:
            run_store: 用于持久化和查询 Run 的存储
            chat_service: 聊天服务实例，用于发送运行取消信号
        """
        self._run_store = run_store  # 保存 Run 存储引用
        self._chat_service = chat_service  # 保存聊天服务引用

    async def get_run(self, run_id: str) -> Run | AppError:  # 获取 Run
        """根据 run_id 查询 Run。

        Args:
            run_id: Run 唯一标识

        Returns:
            Run 实例（如果存在）或 AppError（如果不存在）
        """
        logger.debug("查询 Run: run_id=%s", run_id)

        # 从存储查询
        run = await self._run_store.get_run(run_id)  # 查询 Run

        if run is None:  # Run 不存在
            logger.error("Run 不存在: run_id=%s", run_id)
            return AppError(  # 返回错误对象
                error_code=ErrorCode.RUN_NOT_FOUND,  # 错误码：Run 不存在
                message=f"Run {run_id} not found",  # 错误消息
            )

        logger.debug("查询 Run 成功: run_id=%s, status=%s", run_id, run.status.value)
        return run  # 返回 Run 实例

    def cancel_run(self, run_id: str) -> bool:  # 取消 Run
        """代理到 ChatService 发送取消信号。

        Args:
            run_id: 要取消的运行 ID

        Returns:
            bool: 是否成功发出取消信号
        """
        logger.info("请求取消 Run: run_id=%s", run_id)
        return self._chat_service.cancel_run(run_id)
