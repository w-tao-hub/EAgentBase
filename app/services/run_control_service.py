from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.core.models.run import Run
from app.core.models.error import AppError, ErrorCode

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.core.ports.stores import RunStore


class RunControlService:
    """Run 控制服务。

    负责 Run 的查询和管理，包括：
    1. 根据 run_id 查询 Run 详情
    2. 返回类型化的结果（Run 或 AppError）
    """

    def __init__(
        self,
        run_store: "RunStore",
        chat_service: object,
    ) -> None:
        """初始化 RunControlService。

        Args:
            run_store: 用于持久化和查询 Run 的存储
            chat_service: 聊天服务实例，用于发送运行取消信号
        """
        self._run_store = run_store
        self._chat_service = chat_service

    async def get_run(self, run_id: str) -> Run | AppError:
        """根据 run_id 查询 Run。

        Args:
            run_id: Run 唯一标识

        Returns:
            Run 实例（如果存在）或 AppError（如果不存在）
        """
        logger.debug("查询 Run: run_id=%s", run_id)

        run = await self._run_store.get_run(run_id)

        if run is None:
            logger.error("Run 不存在: run_id=%s", run_id)
            return AppError(
                error_code=ErrorCode.RUN_NOT_FOUND,
                message=f"Run {run_id} not found",
            )

        logger.debug("查询 Run 成功: run_id=%s, status=%s", run_id, run.status.value)
        return run

    def cancel_run(self, run_id: str) -> bool:
        """代理到 ChatService 发送取消信号。

        Args:
            run_id: 要取消的运行 ID

        Returns:
            bool: 是否成功发出取消信号
        """
        logger.info("请求取消 Run: run_id=%s", run_id)
        return self._chat_service.cancel_run(run_id)
