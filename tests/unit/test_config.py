"""配置读取行为测试。"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.bootstrap.factory import load_settings
from app.config import Settings


def _clear_all_env_vars(monkeypatch) -> None:
    """清除所有可能影响测试的项目环境变量，确保测试独立性。

    项目 .env 文件中定义的所有变量都需要在此处清除，防止测试时
    读取到项目实际配置而非预期默认值或测试指定值。
    """
    env_vars_to_clear = [
        # 应用入口配置
        "APP_HOST",
        "APP_PORT",
        "APP_ENV",
        # Redis 配置
        "REDIS_MODE",
        "REDIS_URL",
        "REDIS_SENTINEL_NODES",
        "REDIS_SENTINEL_MASTER_NAME",
        "REDIS_DB",
        "REDIS_USERNAME",
        "REDIS_PASSWORD",
        "REDIS_KEY_PREFIX",
        "SESSION_LOCK_TTL_SECONDS",
        # LiteLLM 配置
        "LITELLM_TIMEOUT_SECONDS",
        "MASTER_AGENT_MODEL",
        "MASTER_AGENT_TEMPERATURE",
        "MASTER_AGENT_REASONING_EFFORT",
        # CORS 配置
        "CORS_ALLOW_ORIGINS",
        "CORS_ALLOW_CREDENTIALS",
        "CORS_ALLOW_METHODS",
        "CORS_ALLOW_HEADERS",
        # Uvicorn 配置
        "UVICORN_WORKERS",
        "UVICORN_TIMEOUT_KEEP_ALIVE",
        "UVICORN_FORWARDED_ALLOW_IPS",
        "UVICORN_ACCESS_LOG",
        # 日志配置
        "LOG_LEVEL",
        "LOG_DIR",
        "LOG_FILE_NAME",
        "LOG_CONSOLE_OUTPUT",
        "LOG_QUEUE_MAXSIZE",
        # 工具工作区与 MCP 配置路径
        "WORKSPACE_ROOT",
        "MCP_SERVERS_CONFIG_PATH",
        # 模型服务商凭据
        "DEEPSEEK_API_KEY",
    ]
    for var in env_vars_to_clear:
        monkeypatch.delenv(var, raising=False)


def test_settings_reads_env_and_defaults(monkeypatch, tmp_path: Path) -> None:
    """验证 Settings 能读取环境变量，并为其余字段提供默认值。"""
    # 切换到临时目录，避免项目 .env 文件干扰默认值断言
    monkeypatch.chdir(tmp_path)

    # 清除所有可能干扰默认值断言的环境变量
    _clear_all_env_vars(monkeypatch)

    # 这个用例只显式设置 `REDIS_URL`，目的是验证：
    # 1. 必填配置可以从环境变量读入；
    # 2. 其余未设置字段会稳定回落到约定默认值。
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/9")

    # 直接实例化 `Settings`，确认对象构造阶段就完成环境变量解析与默认值填充。
    settings = Settings()

    # 下列断言共同锁定 Task 1 的配置契约：
    # Redis 连接串必须读取环境变量，其余配置必须保持设计文档约定的默认值。
    assert settings.redis_mode == "single"
    assert settings.redis_url == "redis://localhost:6379/9"
    assert settings.redis_sentinel_nodes == []
    assert settings.redis_sentinel_master_name is None
    assert settings.redis_db == 0
    assert settings.redis_key_prefix == "agent"
    assert settings.session_lock_ttl_seconds == 30
    assert settings.app_env == "dev"
    assert settings.litellm_timeout_seconds == 60
    assert settings.master_agent_model == "deepseek/deepseek-v4-flash"
    assert settings.master_agent_reasoning_effort == "high"
    assert settings.log_queue_maxsize == 5000


def test_settings_reads_values_from_dotenv_file(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """验证未设置进程环境变量时，Settings 会从当前目录的 `.env` 读取配置。"""
    # 这里故意不注入 `REDIS_URL` 进程环境变量，而是只写入临时 `.env`。
    # 如果 `Settings` 没有把 `.env` 接成真实配置来源，这个用例会在实例化阶段直接失败。
    monkeypatch.chdir(tmp_path)

    # 清除所有可能干扰测试的环境变量，确保从 .env 文件读取
    _clear_all_env_vars(monkeypatch)

    env_file = tmp_path / ".env"
    env_file.write_text(
        "REDIS_URL=redis://dotenv-host:6379/1\nAPP_PORT=9000\n",
        encoding="utf-8",
    )

    settings = Settings()

    assert settings.redis_url == "redis://dotenv-host:6379/1"
    assert settings.app_port == 9000


def test_environment_variables_override_dotenv_values(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """验证进程环境变量优先级高于 `.env` 文件中的同名配置。"""
    # 该用例锁定官方约定的优先级：环境变量始终覆盖 `.env` 中的值。
    monkeypatch.chdir(tmp_path)

    # 清除所有可能干扰测试的环境变量
    _clear_all_env_vars(monkeypatch)

    env_file = tmp_path / ".env"
    env_file.write_text(
        "REDIS_URL=redis://dotenv-host:6379/1\nAPP_PORT=9000\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("REDIS_URL", "redis://env-host:6379/9")
    monkeypatch.setenv("APP_PORT", "9100")

    settings = Settings()

    assert settings.redis_url == "redis://env-host:6379/9"
    assert settings.app_port == 9100


def test_settings_reads_sentinel_config_from_env(monkeypatch, tmp_path: Path) -> None:
    """验证 Sentinel 模式配置可以从环境变量正确读取。"""
    monkeypatch.chdir(tmp_path)
    _clear_all_env_vars(monkeypatch)

    monkeypatch.setenv("REDIS_MODE", "sentinel")
    monkeypatch.setenv("REDIS_SENTINEL_NODES", "10.0.0.1:26379,10.0.0.2:26379")
    monkeypatch.setenv("REDIS_SENTINEL_MASTER_NAME", "mymaster")
    monkeypatch.setenv("REDIS_DB", "5")
    monkeypatch.setenv("REDIS_USERNAME", "sentinel-user")
    monkeypatch.setenv("REDIS_PASSWORD", "sentinel-pass")

    settings = Settings()

    assert settings.redis_mode == "sentinel"
    assert settings.redis_url is None
    assert settings.redis_sentinel_nodes == ["10.0.0.1:26379", "10.0.0.2:26379"]
    assert settings.redis_sentinel_master_name == "mymaster"
    assert settings.redis_db == 5
    assert settings.redis_username == "sentinel-user"
    assert settings.redis_password == "sentinel-pass"


@pytest.mark.parametrize(
    ("env_vars", "expected_message"),
    [
        (
            {
                "REDIS_MODE": "sentinel",
                "REDIS_SENTINEL_MASTER_NAME": "mymaster",
            },
            "REDIS_MODE=sentinel 时必须提供 REDIS_SENTINEL_NODES",
        ),
        (
            {
                "REDIS_MODE": "sentinel",
                "REDIS_SENTINEL_NODES": "10.0.0.1:26379",
            },
            "REDIS_MODE=sentinel 时必须提供 REDIS_SENTINEL_MASTER_NAME",
        ),
    ],
)
def test_settings_requires_complete_sentinel_config(
    monkeypatch,
    tmp_path: Path,
    env_vars: dict[str, str],
    expected_message: str,
) -> None:
    """验证 Sentinel 模式下缺少关键配置会在构造阶段失败。"""
    monkeypatch.chdir(tmp_path)
    _clear_all_env_vars(monkeypatch)

    for key, value in env_vars.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(ValidationError, match=expected_message):
        Settings()


def test_load_settings_anchors_dotenv_and_relative_paths_to_project_root(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """验证 bootstrap 配置加载会固定使用项目根目录并归一化关键相对路径。"""
    # 先切到一个与项目根无关的工作目录，
    # 模拟 systemd 或外部脚本从任意 `cwd` 启动服务的真实场景。
    outside_cwd = tmp_path / "outside-cwd"
    outside_cwd.mkdir()
    monkeypatch.chdir(outside_cwd)

    # 清理进程环境变量，确保本用例只依赖临时项目根目录下的 `.env`。
    _clear_all_env_vars(monkeypatch)

    # 构造一个最小“项目根目录”替身，
    # 用于验证 `.env`、日志目录、工作区和 MCP 配置都以这里为锚点。
    project_root = tmp_path / "project-root"
    project_root.mkdir()
    (project_root / ".env").write_text(
        "\n".join(
            [
                "REDIS_URL=redis://root-dotenv:6379/7",
                "LOG_DIR=runtime_logs",
                "WORKSPACE_ROOT=workspace",
                "MCP_SERVERS_CONFIG_PATH=config/mcp_servers.json",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    settings = load_settings(project_root=project_root)

    # 断言 `.env` 的读取来源已经稳定锚定到显式传入的项目根目录，
    # 并且关键相对路径字段都已经转换成可直接使用的绝对路径。
    assert settings.redis_url == "redis://root-dotenv:6379/7"
    assert settings.log_dir == str((project_root / "runtime_logs").resolve())
    assert settings.workspace_root == str((project_root / "workspace").resolve())
    assert settings.mcp_servers_config_path == str(
        (project_root / "config/mcp_servers.json").resolve()
    )


def test_settings_no_longer_exposes_master_agent_identity_fields(monkeypatch, tmp_path: Path) -> None:
    """验证主代理名称和 ID 已经从环境配置迁移到代码内置定义。"""
    # 切换到临时目录，避免项目 .env 文件干扰
    monkeypatch.chdir(tmp_path)
    # 清除所有可能干扰的环境变量
    _clear_all_env_vars(monkeypatch)
    # 设置必要的 Redis 连接信息
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/9")
    # 设置旧的环境变量，验证它们不再被读取为配置字段
    monkeypatch.setenv("MASTER_AGENT_ID", "env-master")
    monkeypatch.setenv("MASTER_AGENT_NAME", "Env Master")

    settings = Settings()

    # 验证旧的主代理身份字段已从配置中移除
    assert not hasattr(settings, "master_agent_id")
    assert not hasattr(settings, "master_agent_name")
    # 验证模型配置字段仍然存在且返回正确的默认值
    assert settings.master_agent_model == "deepseek/deepseek-v4-flash"
