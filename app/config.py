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

    # 先从当前工作目录下的 `.env` 读取本地开发配置，再继续读取进程环境变量。
    # `pydantic-settings` 会保持“环境变量优先于 `.env`”的覆盖顺序，这正符合当前设计要求。
    # 同时忽略无关环境变量，避免外部运行环境中的额外配置影响当前对象初始化。
    model_config = SettingsConfigDict(
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    # 监听地址、监听端口与环境标识属于应用入口配置。
    # 这里先给出稳定默认值，后续 FastAPI 启动入口会直接复用这组字段。
    app_host: str = Field(default="127.0.0.1", min_length=1)
    app_port: int = Field(default=8000, ge=1, le=65535)
    app_env: str = Field(default="dev", pattern=r"^(dev|prod)$")  # 运行环境：dev 或 prod

    # Redis 连接地址是当前版本唯一必填配置。
    # v0 的会话、运行状态与会话锁都会依赖这条连接串。
    redis_url: str = Field(min_length=1)

    # Redis key 前缀用于隔离不同项目或环境的键空间，避免不同服务共用 Redis 时互相污染。
    redis_key_prefix: str = Field(default="agent", min_length=1)

    # 会话锁 TTL 的单位是秒。
    # 该值既用于首次加锁，也作为后台心跳续期时刷新的目标 TTL。
    # 具体取值由部署侧根据并发规模与冲突恢复时间要求自行权衡。
    session_lock_ttl_seconds: int = Field(default=30, ge=1)

    # LiteLLM 超时时间的单位同样是秒。
    # 当前只提供统一超时配置，后续具体模型适配层会读取该值。
    litellm_timeout_seconds: int = Field(default=60, ge=1)

    # 上下文输入 token 阀值。
    # `0` 表示关闭基于 token 的上下文压缩能力。
    # 大于 `0` 时，ChatService 会在送模型前按该阀值判断是否需要压缩历史上下文。
    context_token_threshold: int = Field(default=0, ge=0)

    # 主智能体 ID。
    # 该字段用于标识默认主智能体实体，并作为会话创建时绑定的 agent_id 默认值。
    master_agent_id: str = Field(default="master-agent", min_length=1)

    # 主智能体展示名称。
    # 该字段用于 API 返回与运行时上下文中的可读名称，默认沿用原有文件化配置。
    master_agent_name: str = Field(default="Master Agent", min_length=1)

    # 主智能体模型标识。
    # 该字段用于驱动 LiteLLM 选择上游模型。
    # 当前默认值切到 DeepSeek V4 Flash，新协议默认开启 thinking。
    master_agent_model: str = Field(default="deepseek/deepseek-v4-flash", min_length=1)

    # 主智能体温度参数。
    # 该字段控制模型采样随机性，默认值 0.2 与此前 master_agent.json 中的行为保持一致。
    master_agent_temperature: float = Field(default=0.2)

    # 主智能体思考强度配置。
    # DeepSeek V4 thinking 模型当前仅允许 high / max 两档，
    # 因此这里直接在配置层做枚举校验，避免非法值跑到请求阶段才失败。
    master_agent_reasoning_effort: str = Field(default="high", pattern=r"^(high|max)$")

    # Run 记录在 Redis 中的过期时间（秒）。
    # 默认 7 天，用于自动清理历史 Run 数据。
    run_ttl_seconds: int = Field(default=604800, ge=1)

    # ===== 日志配置 =====
    # 日志级别，支持 debug、info、error 三级。
    # debug: 调试信息，最详细；info: 一般信息（推荐）；error: 错误信息。
    log_level: str = Field(default="info", pattern=r"^(debug|info|error)$")
    # 日志文件存放目录，相对于项目根目录。
    log_dir: str = Field(default="logs")
    # 日志文件名前缀，实际文件名为 {log_file_name}_YYYYMMDD.log。
    log_file_name: str = Field(default="app")
    # 兼容旧配置保留。
    # 当前日志输出策略固定为：开发环境写 stdout + 文件，生产环境只写文件。
    log_console_output: bool = Field(default=True)
    # 单个日志文件最大大小（字节），超过后即使未到午夜也会轮转。默认 10MB。
    log_max_bytes: int = Field(default=10 * 1024 * 1024)
    # 日志异步队列最大容量。
    # 超过容量后，DEBUG / INFO 会被直接丢弃，WARNING / ERROR 尝试优先保底。
    log_queue_maxsize: int = Field(default=5000, ge=1)

    # ===== CORS 跨域配置 =====
    # 允许的源地址列表，默认允许所有源（开发环境使用，生产环境应配置具体域名）。
    cors_allow_origins: list[str] = Field(default=["*"])
    # 是否允许携带凭证（如 Cookie、Authorization 头等）。
    cors_allow_credentials: bool = Field(default=True)
    # 允许的 HTTP 方法列表，默认允许所有方法。
    cors_allow_methods: list[str] = Field(default=["*"])
    # 允许的 HTTP 请求头列表，默认允许所有请求头。
    cors_allow_headers: list[str] = Field(default=["*"])

    # ===== Uvicorn 服务器配置 =====
    # Uvicorn 工作进程数，生产环境使用，默认等于 CPU 核心数。
    uvicorn_workers: int = Field(default_factory=_get_cpu_count, ge=1)
    # HTTP keep-alive 连接超时时间（秒）。
    uvicorn_timeout_keep_alive: int = Field(default=5, ge=1)
    # 信任的代理服务器 IP 地址，使用 * 表示信任所有。
    uvicorn_forwarded_allow_ips: str = Field(default="127.0.0.1")
    # 是否启用访问日志。
    uvicorn_access_log: bool = Field(default=True)

    # AgentLoop 的最大轮数限制。
    # 用于控制单个会话中 Agent 与模型交互的最大轮数，防止无限循环。
    agent_max_turns: int = Field(default=10, ge=1)

    # RunPythonScriptTool 的工作区根目录路径。
    # 指定 python 脚本执行的基准目录，默认为当前目录 "."。
    workspace_root: str = Field(default=".", min_length=1)

    # MCP 服务配置文件路径。
    # 当前约定使用项目根目录下的 `mcp_servers.json` 作为默认配置文件，
    # 以便同时承载多服务与多传输形态的配置。
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
        # 开发环境强制单进程，热加载；生产环境使用多进程
        if self.is_dev:
            workers = 1
            reload_enabled = True if reload is None else reload
        else:
            workers = self.uvicorn_workers
            reload_enabled = False if reload is None else reload

        return {
            # 公开启动入口已经迁移到 bootstrap 层；
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
