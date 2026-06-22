"""
子代理挂载配置校验——边界用例

验证：
1. 挂载到未知主代理 → 启动期配置错误
2. 空挂载列表 → 配置错误
"""
import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_project_root))

from app.config import Settings
from app.bootstrap.container import Container
from app.core.models.error import ErrorCode


def test_unknown_master_in_mount() -> None:
    """验证挂载未知主代理导致启动期错误。"""
    print("测试: 子代理挂载到未知主代理 → 应抛 INVALID_MASTER_AGENT_CONFIG")
    try:
        Container._resolve_mount_master_agents(
            mount_master_agents=("default", "ghost_master"),
            known_master_names={"default", "plan"},
            child_name="TestAgent",
        )
        print("  ❌ 未抛出异常！")
        sys.exit(1)
    except ValueError as e:
        if ErrorCode.INVALID_MASTER_AGENT_CONFIG.value in str(e) and "ghost_master" in str(e):
            print(f"  ✅ 正确拒绝: {e}")
        else:
            print(f"  ❌ 错误信息不正确: {e}")
            sys.exit(1)


def test_empty_mount_list() -> None:
    """验证空挂载列表导致错误。"""
    print("测试: 空挂载列表 → 应抛 INVALID_MASTER_AGENT_CONFIG")
    try:
        Container._resolve_mount_master_agents(
            mount_master_agents=(),
            known_master_names={"default", "plan"},
            child_name="TestAgent",
        )
        print("  ❌ 未抛出异常！")
        sys.exit(1)
    except ValueError as e:
        if ErrorCode.INVALID_MASTER_AGENT_CONFIG.value in str(e) and "为空" in str(e):
            print(f"  ✅ 正确拒绝: {e}")
        else:
            print(f"  ❌ 错误信息不正确: {e}")
            sys.exit(1)


def test_none_defaults_to_default() -> None:
    """验证 None 默认挂载到 default。"""
    print("测试: mount_master_agents=None → 默认挂载到 default")
    result = Container._resolve_mount_master_agents(
        mount_master_agents=None,
        known_master_names={"default", "plan"},
        child_name="TestAgent",
    )
    if result == ("default",):
        print(f"  ✅ 正确: {result}")
    else:
        print(f"  ❌ 期望 ('default',) 实际 {result}")
        sys.exit(1)


def test_explicit_list() -> None:
    """验证显式列表正确传递。"""
    print("测试: mount_master_agents=('default','plan') → 直接返回")
    result = Container._resolve_mount_master_agents(
        mount_master_agents=("default", "plan"),
        known_master_names={"default", "plan"},
        child_name="TestAgent",
    )
    if result == ("default", "plan"):
        print(f"  ✅ 正确: {result}")
    else:
        print(f"  ❌ 期望 ('default', 'plan') 实际 {result}")
        sys.exit(1)


if __name__ == "__main__":
    print("=" * 50)
    print("  子代理挂载配置校验边界测试")
    print("=" * 50)
    test_unknown_master_in_mount()
    test_empty_mount_list()
    test_none_defaults_to_default()
    test_explicit_list()
    print(f"\n{'='*50}")
    print("🎉 全部边界用例通过！")
