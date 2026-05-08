"""RedisRunStore 实现。

提供 Run 状态的 Redis 持久化存储。
"""

from __future__ import annotations  # 启用未来注解

from typing import TYPE_CHECKING, Any  # 导入类型检查标记

from app.core.models.run import ExecutionMode, Run, RunStatus, RunType  # 导入 Run 模型和状态枚举
from app.core.models.error import ErrorCode  # 导入错误码枚举
from app.infra.logging import get_logger  # 导入日志获取函数

# 获取模块级日志器
logger = get_logger(__name__)

if TYPE_CHECKING:  # 仅在类型检查时导入
    from redis.asyncio import Redis  # 异步 Redis 客户端类型


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

    def __init__(self, redis: Redis, key_prefix: str = "agent") -> None:  # 构造函数
        """初始化 RunStore。

        Args:
            redis: Redis 异步客户端实例
            key_prefix: Redis key 前缀，用于命名空间隔离
        """
        self._redis = redis  # 保存 Redis 客户端引用
        self._key_prefix = key_prefix  # 保存 key 前缀
        # 尝试注册 Lua 脚本，fakeredis 不支持则回退到非原子实现
        self._create_script = self._try_register_script(redis)  # 尝试注册 Lua 脚本

    def _try_register_script(self, redis: Redis):  # 尝试注册 Lua 脚本
        """尝试注册 Lua 脚本，如果不支持则返回 None。"""
        try:
            return redis.register_script(self._CREATE_IF_NOT_EXISTS_LUA)  # 注册 Lua 脚本
        except Exception:  # fakeredis 等不支持 eval 的客户端
            return None  # 返回 None 表示使用回退实现

    def _run_key(self, run_id: str) -> str:  # 生成 Run key
        """生成 Run 的 Redis key。"""
        return f"{self._key_prefix}:run:{run_id}"  # 拼接 key

    def _run_to_dict(self, run: Run) -> dict:  # Run 转字典
        """将 Run 实例序列化为存储字典。

        Args:
            run: Run 实例

        Returns:
            存储字典
        """
        import json  # 导入 JSON 模块用于序列化 metadata

        data = {  # 构造基础数据
            "run_id": run.run_id,  # Run ID
            "session_id": run.session_id,  # 会话 ID
            "status": run.status.value,  # 状态值（字符串）
            "run_type": run.run_type.value,  # Run 类型（master/child）
            "execution_mode": run.execution_mode.value,  # 执行模式（foreground/background）
            "created_at": run.created_at.isoformat(),  # ISO 格式创建时间
            "updated_at": run.updated_at.isoformat(),  # ISO 格式更新时间
        }
        if run.agent_id is not None:  # 如果有代理 ID
            data["agent_id"] = run.agent_id  # 保存代理 ID
        if run.parent_run_id is not None:  # 如果有父 Run ID
            data["parent_run_id"] = run.parent_run_id  # 保存父 Run ID
        if run.child_id is not None:  # 如果有会话内稳定 child_id
            data["child_id"] = run.child_id  # 保存 child_id
        if run.tool_call_id is not None:  # 如果有关联工具调用 ID
            data["tool_call_id"] = run.tool_call_id  # 保存工具调用 ID
        # 可选字段：仅在不为 None 时添加
        if run.finished_at is not None:  # 如果有完成时间
            data["finished_at"] = run.finished_at.isoformat()  # ISO 格式完成时间
        if run.output is not None:  # 如果有输出
            data["output"] = run.output  # 输出内容
        if run.error_code is not None:  # 如果有错误码
            data["error_code"] = run.error_code.value  # 错误码值
        if run.error_message is not None:  # 如果有错误消息
            data["error_message"] = run.error_message  # 错误消息
        if run.metadata is not None:  # 如果有元数据
            data["metadata"] = json.dumps(run.metadata, ensure_ascii=False)  # 序列化为 JSON 字符串
        return data  # 返回数据字典

    def _dict_to_run(self, data: dict) -> Run:  # 字典转 Run
        """从存储字典反序列化为 Run 实例。

        Args:
            data: 存储字典

        Returns:
            Run 实例
        """
        from datetime import datetime  # 导入 datetime
        import json  # 导入 JSON 模块用于反序列化 metadata

        # 先解析创建时间，后续 legacy updated_at 缺失时要回退到这个值
        created_at = datetime.fromisoformat(data["created_at"])  # 解析创建时间

        # 解析可选字段
        finished_at = None  # 初始化完成时间
        if "finished_at" in data and data["finished_at"]:  # 如果有完成时间
            finished_at = datetime.fromisoformat(data["finished_at"])  # 解析 ISO 时间
        updated_at = created_at  # 旧数据未写入 updated_at 时，默认沿用 created_at
        if "updated_at" in data and data["updated_at"]:  # 如果有更新时间
            updated_at = datetime.fromisoformat(data["updated_at"])  # 解析更新时间
        output = data.get("output")  # 获取输出（可能为 None）
        error_code = None  # 初始化错误码
        if "error_code" in data and data["error_code"]:  # 如果有错误码
            error_code = ErrorCode(data["error_code"])  # 构造错误码枚举
        error_message = data.get("error_message")  # 获取错误消息（可能为 None）
        metadata = None  # 初始化元数据
        if "metadata" in data and data["metadata"]:  # 如果有元数据
            try:
                metadata = json.loads(data["metadata"])  # 从 JSON 字符串反序列化为 dict
            except json.JSONDecodeError:
                metadata = None  # 解析失败时返回 None
        return Run(  # 构造 Run 实例
            run_id=data["run_id"],  # Run ID
            session_id=data["session_id"],  # 会话 ID
            status=RunStatus(data["status"]),  # 状态枚举
            agent_id=data.get("agent_id"),  # 代理 ID（旧数据可能没有）
            run_type=RunType(data.get("run_type", RunType.MASTER.value)),  # 兼容旧数据默认 master
            parent_run_id=data.get("parent_run_id"),  # 父 Run ID（仅 child 有）
            child_id=data.get("child_id"),  # 会话内稳定 child_id（仅 child 有）
            tool_call_id=data.get("tool_call_id"),  # 触发 child 的 tool_call_id（仅 child 有）
            execution_mode=ExecutionMode(
                data.get("execution_mode", ExecutionMode.FOREGROUND.value)
            ),  # 兼容旧数据默认 foreground
            created_at=created_at,  # 解析后的创建时间
            updated_at=updated_at,  # 解析后的更新时间
            finished_at=finished_at,  # 完成时间
            output=output,  # 输出
            error_code=error_code,  # 错误码
            error_message=error_message,  # 错误消息
            metadata=metadata,  # 元数据
        )

    async def create_run(self, run: Run, ttl_seconds: int | None = None) -> None:  # 创建 Run
        """创建 Run 记录。

        使用 Lua 脚本保证 exists + hset 的原子性，避免高并发下的竞态条件。
        如果 Redis 客户端不支持 Lua（如 fakeredis），回退到非原子实现。

        Args:
            run: 要创建的 Run 实例
            ttl_seconds: 过期时间（秒），None 表示不过期

        Raises:
            ValueError: 如果 run_id 已存在
        """
        run_key = self._run_key(run.run_id)  # 获取 Run key
        data = self._run_to_dict(run)  # 序列化为字典

        if self._create_script is not None:  # Lua 脚本可用（真实 Redis）
            # 将 mapping dict 展平为 [field1, value1, field2, value2, ...] 供 Lua unpack 使用
            flat_args = []  # 展平的参数列表
            for k, v in data.items():  # 遍历字典
                flat_args.extend([k, str(v)])  # 添加字段名和值（转为字符串）
            try:  # 尝试执行 Lua 脚本
                created = await self._create_script(  # 执行已注册的 Lua 脚本
                    keys=[run_key],  # KEYS 数组
                    args=flat_args,  # ARGV: 展平的 field-value 对
                )
                if created == 0:  # 如果返回 0，表示 key 已存在
                    raise ValueError(f"Run {run.run_id} already exists")  # 抛出异常
                # 设置过期时间
                if ttl_seconds is not None:
                    await self._redis.expire(run_key, ttl_seconds)
                return  # 创建成功，直接返回
            except ValueError:  # 重复 key 异常需要继续向上抛出
                raise  # 重新抛出
            except Exception:  # Lua 脚本执行失败，回退到非原子实现
                pass  # 继续执行下面的回退逻辑

        # 回退实现（fakeredis 等不支持 Lua 的场景）：非原子的 exists + hset
        exists = await self._redis.exists(run_key)  # 检查 key 是否存在
        if exists:  # 如果已存在
            raise ValueError(f"Run {run.run_id} already exists")  # 抛出异常
        await self._redis.hset(run_key, mapping=data)  # 存储到 Redis
        # 设置过期时间
        if ttl_seconds is not None:
            await self._redis.expire(run_key, ttl_seconds)

    def queue_create_run(self, pipeline: Any, run: Run, ttl_seconds: int | None = None) -> None:
        """向 Redis pipeline 中排入一条 run 建档命令。

        Args:
            pipeline: Redis pipeline 对象
            run: 要创建的 Run 实例
            ttl_seconds: 过期时间（秒），None 表示不过期
        """
        run_key = self._run_key(run.run_id)  # 统一先生成 key，避免 HSET/EXPIRE 重复拼接
        pipeline.hset(run_key, mapping=self._run_to_dict(run))  # 先写入 Run 字段
        if ttl_seconds is not None:  # 仅在显式配置 TTL 时才补充过期控制
            pipeline.expire(run_key, ttl_seconds)  # 在同一条 pipeline 中补充 EXPIRE，保证建档路径也能生效 TTL

    async def get_run(self, run_id: str) -> Run | None:  # 获取 Run
        """读取 Run 记录。

        Args:
            run_id: Run 唯一标识

        Returns:
            Run 实例，如果不存在则返回 None
        """
        run_key = self._run_key(run_id)  # 获取 Run key
        # 使用 HGETALL 获取 Run 数据
        data = await self._redis.hgetall(run_key)  # 从 Redis 读取
        if not data:  # 如果数据为空
            logger.debug("Run 不存在: run_id=%s", run_id)
            return None  # 返回 None 表示不存在
        logger.debug("Run 查询成功: run_id=%s", run_id)
        return self._dict_to_run(data)  # 反序列化为 Run 实例

    async def update_run(self, run: Run) -> None:  # 更新 Run
        """更新 Run 记录。

        警告：此方法会覆盖所有字段。如需部分更新，请使用 update_run_fields。

        Args:
            run: 要更新的 Run 实例
        """
        run_key = self._run_key(run.run_id)  # 获取 Run key
        data = self._run_to_dict(run)  # 序列化为字典
        # 使用 HSET 更新 Run 数据（覆盖原有数据）
        await self._redis.hset(run_key, mapping=data)  # 更新到 Redis

    async def update_run_fields(
        self,
        run_id: str,
        status: RunStatus,
        finished_at: datetime,
        output: str | None = None,
        error_code: ErrorCode | None = None,
        error_message: str | None = None,
    ) -> None:
        """部分更新 Run 的终态字段，其他字段保持不变。

        此方法仅更新指定的终态字段（status、finished_at 及可选的 output/error_code/error_message），
        不会触碰 run_id、session_id、created_at、metadata 等其他字段，避免误覆盖。

        Args:
            run_id: Run 唯一标识
            status: 新的状态（COMPLETED 或 FAILED）
            finished_at: 完成时间
            output: 成功输出内容（仅 COMPLETED 时传入）
            error_code: 错误码（仅 FAILED 时传入）
            error_message: 错误描述（仅 FAILED 时传入）
        """
        run_key = self._run_key(run_id)  # 获取 Run key
        # 构造要更新的字段字典
        fields: dict[str, str] = {
            "status": status.value,  # 状态值（字符串）
            "finished_at": finished_at.isoformat(),  # ISO 格式完成时间
            "updated_at": finished_at.isoformat(),  # 终态更新时同步刷新 updated_at
        }
        # 可选字段：仅在不为 None 时添加
        if output is not None:  # 如果有输出
            fields["output"] = output  # 输出内容
        if error_code is not None:  # 如果有错误码
            fields["error_code"] = error_code.value  # 错误码值
        if error_message is not None:  # 如果有错误消息
            fields["error_message"] = error_message  # 错误消息
        # 使用 HSET 更新指定字段，其他字段保持不变
        await self._redis.hset(run_key, mapping=fields)  # 部分更新到 Redis

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
        """向 Redis pipeline 中排入 Run 终态字段更新命令。

        该方法与 `update_run_fields()` 保持同一字段语义，
        区别仅在于这里不会立即执行网络请求，而是交由调用方统一 `execute()`。

        Args:
            pipeline: Redis pipeline 对象
            run_id: Run 唯一标识
            status: 新的状态
            finished_at: 完成时间
            output: 成功输出内容
            error_code: 错误码
            error_message: 错误描述
        """
        run_key = self._run_key(run_id)  # 获取 Run key
        fields: dict[str, str] = {
            "status": status.value,  # 状态值（字符串）
            "finished_at": finished_at.isoformat(),  # ISO 格式完成时间
            "updated_at": finished_at.isoformat(),  # 终态更新时同步刷新 updated_at
        }
        if output is not None:  # 如果有输出
            fields["output"] = output  # 输出内容
        if error_code is not None:  # 如果有错误码
            fields["error_code"] = error_code.value  # 错误码值
        if error_message is not None:  # 如果有错误消息
            fields["error_message"] = error_message  # 错误消息
        pipeline.hset(run_key, mapping=fields)  # 将 HSET 命令排入 pipeline，等待调用方统一执行

    async def delete_run(self, run_id: str) -> int:
        """删除单条 run。"""
        return int(await self._redis.delete(self._run_key(run_id)))

    async def delete_runs(self, run_ids: list[str]) -> int:
        """批量删除多条 run。"""
        if not run_ids:
            return 0
        return int(await self._redis.delete(*[self._run_key(run_id) for run_id in run_ids]))
