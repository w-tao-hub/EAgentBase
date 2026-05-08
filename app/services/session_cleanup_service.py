"""SessionCleanupService 实现。

负责按 session_id 级联删除会话相关的主上下文、child 上下文、run 记录与大工具结果。
"""

from __future__ import annotations


class SessionCleanupService:
    """会话级联删除服务。"""

    def __init__(self, session_store, run_store, tool_result_store) -> None:
        """初始化会话级联删除服务。"""
        self._session_store = session_store
        self._run_store = run_store
        self._tool_result_store = tool_result_store

    async def delete_session_cascade(self, session_id: str) -> dict[str, int]:
        """级联删除某个 session 的全部已知相关 key。"""
        run_ids = await self._session_store.list_session_run_ids(session_id)
        child_ids = await self._session_store.list_session_child_ids(session_id)

        deleted_child_contexts = 0
        for child_id in child_ids:
            deleted_child_contexts += await self._session_store.delete_child_context(session_id, child_id)

        deleted_main_context = await self._session_store.delete_session_main_context(session_id)
        deleted_runs = await self._run_store.delete_runs(run_ids)
        deleted_tool_results = await self._tool_result_store.delete_session_results(session_id)
        deleted_metadata_and_indices = await self._session_store.delete_session_metadata_and_indices(session_id)

        return {
            "deleted_main_context_keys": deleted_main_context,
            "deleted_child_context_keys": deleted_child_contexts,
            "deleted_runs": deleted_runs,
            "deleted_tool_results": deleted_tool_results,
            "deleted_metadata_and_index_keys": deleted_metadata_and_indices,
        }
