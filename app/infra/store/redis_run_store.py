"""RedisRunStore 实现。

提供 Run 状态的 Redis 持久化存储。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.core.models.run import ExecutionMode, Run, RunStatus, RunType
from app.core.models.error import ErrorCode
from app.infra.logging import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    from redis.asyncio import Redis


class RedisRunStore:
    """基于 Redis 的 Run 存储实现。

    使用 Hash 存储 Run 的所有字段。
    Key 结构：{prefix}:run:{run_id}
    """

    # Lua 脚本：原子的"不存在才写入"（HSET NX 语义）
    # Redis HSET 不支持 NX 选项，通过 Lua 脚本将 exists + hset 合并为原子操作
    _CREATE_IF_NOT_EXISTS_LUA = """
        if redis.call('exists', KEYS[1]) == 0 then
            redis.call('hset', KEYS[1], unpack(ARGV))
            return 1
        else
            return 0
        end
    """

    def __init__(self, redis: Redis, key_prefix: str = "agent") -> None:
        self._redis = redis
        self._key_prefix = key_prefix
        self._create_script = self._try_register_script(redis)

    def _try_register_script(self, redis: Redis):
        try:
            return redis.register_script(self._CREATE_IF_NOT_EXISTS_LUA)
        except Exception:
            return None

    def _run_key(self, run_id: str) -> str:
        return f"{self._key_prefix}:run:{run_id}"

    def _run_to_dict(self, run: Run) -> dict:
        import json

        data = {
            "run_id": run.run_id,
            "session_id": run.session_id,
            "status": run.status.value,
            "run_type": run.run_type.value,
            "execution_mode": run.execution_mode.value,
            "created_at": run.created_at.isoformat(),
            "updated_at": run.updated_at.isoformat(),
        }
        if run.agent_id is not None:
            data["agent_id"] = run.agent_id
        if run.parent_run_id is not None:
            data["parent_run_id"] = run.parent_run_id
        if run.child_id is not None:
            data["child_id"] = run.child_id
        if run.tool_call_id is not None:
            data["tool_call_id"] = run.tool_call_id
        if run.finished_at is not None:
            data["finished_at"] = run.finished_at.isoformat()
        if run.output is not None:
            data["output"] = run.output
        if run.error_code is not None:
            data["error_code"] = run.error_code.value
        if run.error_message is not None:
            data["error_message"] = run.error_message
        if run.metadata is not None:
            data["metadata"] = json.dumps(run.metadata, ensure_ascii=False)
        return data

    def _dict_to_run(self, data: dict) -> Run:
        from datetime import datetime
        import json

        created_at = datetime.fromisoformat(data["created_at"])

        finished_at = None
        if "finished_at" in data and data["finished_at"]:
            finished_at = datetime.fromisoformat(data["finished_at"])
        updated_at = created_at
        if "updated_at" in data and data["updated_at"]:
            updated_at = datetime.fromisoformat(data["updated_at"])
        output = data.get("output")
        error_code = None
        if "error_code" in data and data["error_code"]:
            error_code = ErrorCode(data["error_code"])
        error_message = data.get("error_message")
        metadata = None
        if "metadata" in data and data["metadata"]:
            try:
                metadata = json.loads(data["metadata"])
            except json.JSONDecodeError:
                metadata = None
        return Run(
            run_id=data["run_id"],
            session_id=data["session_id"],
            status=RunStatus(data["status"]),
            agent_id=data.get("agent_id"),
            run_type=RunType(data.get("run_type", RunType.MASTER.value)),
            parent_run_id=data.get("parent_run_id"),
            child_id=data.get("child_id"),
            tool_call_id=data.get("tool_call_id"),
            execution_mode=ExecutionMode(
                data.get("execution_mode", ExecutionMode.FOREGROUND.value)
            ),
            created_at=created_at,
            updated_at=updated_at,
            finished_at=finished_at,
            output=output,
            error_code=error_code,
            error_message=error_message,
            metadata=metadata,
        )

    async def create_run(self, run: Run, ttl_seconds: int | None = None) -> None:
        """创建 Run 记录（Lua 原子 exists+hset，回退非原子实现）。"""
        run_key = self._run_key(run.run_id)
        data = self._run_to_dict(run)

        if self._create_script is not None:
            flat_args = []
            for k, v in data.items():
                flat_args.extend([k, str(v)])
            try:
                created = await self._create_script(
                    keys=[run_key],
                    args=flat_args,
                )
                if created == 0:
                    raise ValueError(f"Run {run.run_id} already exists")
                if ttl_seconds is not None:
                    await self._redis.expire(run_key, ttl_seconds)
                return
            except ValueError:
                raise
            except Exception:
                pass

        exists = await self._redis.exists(run_key)
        if exists:
            raise ValueError(f"Run {run.run_id} already exists")
        await self._redis.hset(run_key, mapping=data)
        if ttl_seconds is not None:
            await self._redis.expire(run_key, ttl_seconds)

    def queue_create_run(self, pipeline: Any, run: Run, ttl_seconds: int | None = None) -> None:
        """向 pipeline 排入 run 建档命令。"""
        run_key = self._run_key(run.run_id)
        pipeline.hset(run_key, mapping=self._run_to_dict(run))
        if ttl_seconds is not None:
            pipeline.expire(run_key, ttl_seconds)

    async def get_run(self, run_id: str) -> Run | None:
        run_key = self._run_key(run_id)
        data = await self._redis.hgetall(run_key)
        if not data:
            logger.debug("Run 不存在: run_id=%s", run_id)
            return None
        logger.debug("Run 查询成功: run_id=%s", run_id)
        return self._dict_to_run(data)

    async def update_run(self, run: Run) -> None:
        run_key = self._run_key(run.run_id)
        data = self._run_to_dict(run)
        await self._redis.hset(run_key, mapping=data)

    async def update_run_fields(
        self,
        run_id: str,
        status: RunStatus,
        finished_at: datetime,
        output: str | None = None,
        error_code: ErrorCode | None = None,
        error_message: str | None = None,
    ) -> None:
        """部分更新 Run 的终态字段。"""
        run_key = self._run_key(run_id)
        fields: dict[str, str] = {
            "status": status.value,
            "finished_at": finished_at.isoformat(),
            "updated_at": finished_at.isoformat(),
        }
        if output is not None:
            fields["output"] = output
        if error_code is not None:
            fields["error_code"] = error_code.value
        if error_message is not None:
            fields["error_message"] = error_message
        await self._redis.hset(run_key, mapping=fields)

    def queue_update_run_fields(
        self,
        pipeline: Any,
        run_id: str,
        status: RunStatus,
        finished_at: datetime,
        output: str | None = None,
        error_code: ErrorCode | None = None,
        error_message: str | None = None,
    ) -> None:
        """向 pipeline 排入 Run 终态字段更新命令。"""
        run_key = self._run_key(run_id)
        fields: dict[str, str] = {
            "status": status.value,
            "finished_at": finished_at.isoformat(),
            "updated_at": finished_at.isoformat(),
        }
        if output is not None:
            fields["output"] = output
        if error_code is not None:
            fields["error_code"] = error_code.value
        if error_message is not None:
            fields["error_message"] = error_message
        pipeline.hset(run_key, mapping=fields)

    async def delete_run(self, run_id: str) -> int:
        return int(await self._redis.delete(self._run_key(run_id)))

    async def delete_runs(self, run_ids: list[str]) -> int:
        if not run_ids:
            return 0
        return int(await self._redis.delete(*[self._run_key(run_id) for run_id in run_ids]))
