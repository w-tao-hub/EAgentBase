"""应用配置定义。"""

import multiprocessing

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    redis_url: str = Field(min_length=1)

    redis_key_prefix: str = Field(default="agent", min_length=1)

    # 用于首次加锁和后台心跳续期。
    session_lock_ttl_seconds: int = Field(default=30, ge=1)

    litellm_timeout_seconds: int = Field(default=60, ge=1)

    # 0 表示关闭上下文压缩，大于 0 时 ChatService 会据此判断是否需要压缩历史上下文。
    context_token_threshold: int = Field(default=0, ge=0)

    # 会话创建时绑定的默认 agent_id。
    master_agent_id: str = Field(default="master-agent", min_length=1)

    # API 返回与运行时上下文中的可读名称。
    master_agent_name: str = Field(default="Master Agent", min_length=1)

    # 驱动 LiteLLM 选择上游模型。默认 DeepSeek V4 Flash，新协议默认开启 thinking。
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
