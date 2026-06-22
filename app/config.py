"""应用配置定义。"""

import json
import multiprocessing
from typing import Annotated, Any

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _get_cpu_count() -> int:
    """获取 CPU 核心数，用于设置默认工作进程数。

    Returns:
        CPU 逻辑核心数量，至少返回 1
    """
    try:
        return multiprocessing.cpu_count()
    except NotImplementedError:
        return 1


class Settings(BaseSettings):
    """集中管理应用配置，并从环境变量读取可覆盖项。"""

    # pydantic-settings 保证环境变量优先于 .env，同时忽略无关环境变量。
    model_config = SettingsConfigDict(
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    app_host: str = Field(default="127.0.0.1", min_length=1)
    app_port: int = Field(default=8000, ge=1, le=65535)
    app_env: str = Field(default="dev", pattern=r"^(dev|prod)$")

    # Redis 支持两种模式：
    # 1. single：沿用单点 REDIS_URL。
    # 2. sentinel：通过 Sentinel 节点列表 + master 名称发现主节点。
    redis_mode: str = Field(default="single", pattern=r"^(single|sentinel)$")

    # 单点模式下使用的 Redis 连接串。
    redis_url: str | None = Field(default=None, min_length=1)

    # Sentinel 模式下使用的 Sentinel 节点列表。
    # 环境变量支持两种写法：
    # 1. 逗号分隔：host1:26379,host2:26379
    # 2. JSON 数组：["host1:26379","host2:26379"]
    redis_sentinel_nodes: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # Sentinel 模式下用于发现主节点的 service name。
    redis_sentinel_master_name: str | None = Field(default=None, min_length=1)

    # Sentinel 模式下主 Redis 使用的逻辑库编号。
    redis_db: int = Field(default=0, ge=0)

    # 当前版本默认让 Sentinel 与 master 共用同一套认证信息。
    redis_username: str | None = Field(default=None, min_length=1)
    redis_password: str | None = Field(default=None, min_length=1)

    redis_key_prefix: str = Field(default="agent", min_length=1)

    # 用于首次加锁和后台心跳续期。
    session_lock_ttl_seconds: int = Field(default=30, ge=1)

    litellm_timeout_seconds: int = Field(default=60, ge=1)

    # 0 表示关闭上下文压缩，大于 0 时 ChatService 会据此判断是否需要压缩历史上下文。
    context_token_threshold: int = Field(default=0, ge=0)

    # 驱动 LiteLLM 选择上游模型。所有主代理和子代理统一使用的模型标识，默认 DeepSeek V4 Flash，新协议默认开启 thinking。
    master_agent_model: str = Field(default="deepseek/deepseek-v4-flash", min_length=1)

    # 控制模型采样随机性，默认值 0.2。
    master_agent_temperature: float = Field(default=0.2)

    # DeepSeek V4 thinking 仅允许 high / max 两档，直接在配置层做枚举校验。
    master_agent_reasoning_effort: str = Field(default="high", pattern=r"^(high|max)$")

    # 用于自动清理历史 Run 数据，默认 7 天。
    run_ttl_seconds: int = Field(default=604800, ge=1)

    # ===== 日志配置 =====
    log_level: str = Field(default="info", pattern=r"^(debug|info|error)$")
    log_dir: str = Field(default="logs")
    log_file_name: str = Field(default="app")
    # 开发环境写 stdout + 文件，生产环境只写文件。
    log_console_output: bool = Field(default=True)
    log_max_bytes: int = Field(default=10 * 1024 * 1024)
    # 超过容量后，DEBUG/INFO 被丢弃，WARNING/ERROR 优先保底。
    log_queue_maxsize: int = Field(default=5000, ge=1)

    # ===== CORS 跨域配置 =====
    cors_allow_origins: list[str] = Field(default=["*"])
    cors_allow_credentials: bool = Field(default=True)
    cors_allow_methods: list[str] = Field(default=["*"])
    cors_allow_headers: list[str] = Field(default=["*"])

    # ===== Uvicorn 服务器配置 =====
    uvicorn_workers: int = Field(default_factory=_get_cpu_count, ge=1)
    uvicorn_timeout_keep_alive: int = Field(default=5, ge=1)
    uvicorn_forwarded_allow_ips: str = Field(default="127.0.0.1")
    uvicorn_access_log: bool = Field(default=True)

    # 防止 Agent 与模型无限循环。
    agent_max_turns: int = Field(default=10, ge=1)

    workspace_root: str = Field(default=".", min_length=1)

    # 使用项目根目录下的 mcp_servers.json 承载多服务与多传输形态配置。
    mcp_servers_config_path: str = Field(default="mcp_servers.json", min_length=1)

    @field_validator("redis_sentinel_nodes", mode="before")
    @classmethod
    def _normalize_redis_sentinel_nodes(cls, value: Any) -> list[str]:
        """把 Sentinel 节点输入统一转换成规范列表。"""
        # 未配置时统一转为空列表，便于后续模式校验。
        if value is None or value == "":
            return []

        # 环境变量最常见是字符串；这里兼容逗号分隔和 JSON 数组两种写法。
        if isinstance(value, str):
            normalized_value = value.strip()
            if normalized_value == "":
                return []
            if normalized_value.startswith("["):
                try:
                    parsed_value = json.loads(normalized_value)
                except json.JSONDecodeError as exc:
                    raise ValueError("REDIS_SENTINEL_NODES JSON 格式不合法") from exc
                value = parsed_value
            else:
                return [node.strip() for node in normalized_value.split(",") if node.strip()]

        # 代码内直接传 list/tuple 时也统一收敛成字符串列表。
        if isinstance(value, (list, tuple)):
            return [str(node).strip() for node in value if str(node).strip()]

        raise ValueError("REDIS_SENTINEL_NODES 必须是字符串或字符串列表")

    @model_validator(mode="after")
    def _validate_redis_mode_settings(self) -> "Settings":
        """按 Redis 模式校验必填配置。"""
        # 单点模式保持现有契约：必须提供 REDIS_URL。
        if self.redis_mode == "single":
            if not self.redis_url:
                raise ValueError("REDIS_MODE=single 时必须提供 REDIS_URL")
            return self

        # Sentinel 模式下，节点列表与 master 名称缺一不可。
        if not self.redis_sentinel_nodes:
            raise ValueError("REDIS_MODE=sentinel 时必须提供 REDIS_SENTINEL_NODES")
        if not self.redis_sentinel_master_name:
            raise ValueError("REDIS_MODE=sentinel 时必须提供 REDIS_SENTINEL_MASTER_NAME")
        return self

    @property
    def is_dev(self) -> bool:
        """判断是否为开发环境。"""
        return self.app_env == "dev"

    def get_uvicorn_config(self, reload: bool | None = None) -> dict:
        """生成 Uvicorn 配置字典。

        开发环境强制单进程并启用热加载；生产环境使用多进程。

        Args:
            reload: 强制指定热加载开关，None 表示根据环境自动判断。

        Returns:
            Uvicorn 配置字典
        """
        if self.is_dev:
            workers = 1
            reload_enabled = True if reload is None else reload
        else:
            workers = self.uvicorn_workers
            reload_enabled = False if reload is None else reload

        return {
            # uvicorn 通过该工厂先完成配置与容器准备，再装配 FastAPI。
            "app": "app.bootstrap.factory:bootstrap_app",
            "host": self.app_host,
            "port": self.app_port,
            "workers": workers,
            "reload": reload_enabled,
            "log_level": self.log_level,
            "timeout_keep_alive": self.uvicorn_timeout_keep_alive,
            "forwarded_allow_ips": self.uvicorn_forwarded_allow_ips,
            "access_log": self.uvicorn_access_log,
            "factory": True,
        }
