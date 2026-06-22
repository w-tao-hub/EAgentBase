"""
子代理挂载可见性——程序化验证

直接检查 Container 中 default 和 plan 主代理各自的 TaskTool child_profiles，
验证子代理挂载配置是否正确生效。

用法: .venv/bin/python tests/curl_test/verify_subagent_mount.py
"""
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
_project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_project_root))

from app.config import Settings
from app.bootstrap.container import Container


def main() -> None:
    """验证子代理挂载配置。"""
    settings = Settings(redis_url="redis://:scm_123@117.72.179.42:6379/0")

    print("创建 Container...")
    container = Container.create(settings=settings)

    provider = container._agent_provider

    # 获取两个主代理的 TaskTool
    default_profile = provider.get_master_profile_by_name("default")
    plan_profile = provider.get_master_profile_by_name("plan")

    default_task_tool = default_profile.tool_registry.get("Task")
    plan_task_tool = plan_profile.tool_registry.get("Task")

    default_visible = set(default_task_tool._child_profiles.keys())
    plan_visible = set(plan_task_tool._child_profiles.keys())

    print(f"\ndefault 主代理可见子代理: {sorted(default_visible)}")
    print(f"plan 主代理可见子代理:    {sorted(plan_visible)}")

    # ── 验证规则 ──────────────────────────────────────────────
    errors: list[str] = []

    # 规则1: Echo 没有 mount_master_agents → 只挂载到 default
    if "Echo" in default_visible:
        print("\n✅ Echo 对 default 可见（默认挂载）")
    else:
        errors.append("Echo 应对 default 可见但不可见")

    if "Echo" not in plan_visible:
        print("✅ Echo 对 plan 不可见（未显式挂载到 plan）")
    else:
        errors.append("Echo 不应对 plan 可见但可见")

    # 规则2: PlanOnlyAgent 只挂载到 plan
    if "PlanOnlyAgent" not in default_visible:
        print("✅ PlanOnlyAgent 对 default 不可见（仅挂载到 plan）")
    else:
        errors.append("PlanOnlyAgent 不应对 default 可见但可见")

    if "PlanOnlyAgent" in plan_visible:
        print("✅ PlanOnlyAgent 对 plan 可见（显式挂载）")
    else:
        errors.append("PlanOnlyAgent 应对 plan 可见但不可见")

    # 规则3: BothAgent 挂载到 default 和 plan
    if "BothAgent" in default_visible:
        print("✅ BothAgent 对 default 可见（显式挂载）")
    else:
        errors.append("BothAgent 应对 default 可见但不可见")

    if "BothAgent" in plan_visible:
        print("✅ BothAgent 对 plan 可见（显式挂载）")
    else:
        errors.append("BothAgent 应对 plan 可见但不可见")

    # 规则4: Worker（默认子代理）没有 mount → 只挂载到 default
    if "Worker" in default_visible:
        print("✅ Worker 对 default 可见（默认挂载）")
    else:
        errors.append("Worker 应对 default 可见但不可见")

    if "Worker" not in plan_visible:
        print("✅ Worker 对 plan 不可见（未显式挂载到 plan）")
    else:
        errors.append("Worker 不应对 plan 可见但可见")

    # ── 检查 plan TaskTool schema ──
    plan_task_schema = plan_task_tool.input_schema
    subagent_desc = plan_task_schema["properties"]["subagent_type"]["description"]
    if "PlanOnlyAgent" in subagent_desc and "BothAgent" in subagent_desc:
        print(f"\n✅ plan TaskTool schema 包含已挂载子代理")
    else:
        errors.append(f"plan TaskTool schema 不完整: {subagent_desc}")

    if "Echo" not in subagent_desc and "Worker" not in subagent_desc:
        print("✅ plan TaskTool schema 不包含未挂载子代理")
    else:
        errors.append(f"plan TaskTool schema 泄露了未挂载子代理: {subagent_desc}")

    # ── 结果 ──────────────────────────────────────────────────
    print(f"\n{'='*50}")
    if errors:
        print(f"❌ {len(errors)} 个验证失败:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    else:
        print("🎉 全部子代理挂载验证通过！")
        print(f"   default 可见: {sorted(default_visible)}")
        print(f"   plan 可见:    {sorted(plan_visible)}")
        sys.exit(0)


if __name__ == "__main__":
    main()
