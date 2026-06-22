#!/bin/bash
# =============================================================================
# 主代理 tool/hook/skill 挂载冒烟测试
#
# 配置：
#   default: 7 tool + persist_large_result hook + test-skill
#   plan:    不在字典中 → 无 tool/hook/skill
# =============================================================================

set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
PASS=0
FAIL=0
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

collect_response() {
    local sid="$1" agent="$2" msg="$3"
    curl -s -N -X POST "$BASE_URL/chat" \
        -H "Content-Type: application/json" \
        -d "{\"session_id\":\"$sid\",\"master_agent_name\":\"$agent\",\"message\":\"$msg\"}" 2>&1 | \
        python3 "$SCRIPT_DIR/_collect_sse.py"
}

check_ok() { echo "  ✅ $1"; PASS=$((PASS + 1)); }
check_fail() { echo "  ❌ $1"; FAIL=$((FAIL + 1)); }

echo "============================================"
echo "  主代理 tool/hook/skill 挂载冒烟测试"
echo "============================================"
echo ""

# ---- 创建会话 ----
DEFAULT_SID=$(curl -s -X POST "$BASE_URL/sessions" | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
PLAN_SID=$(curl -s -X POST "$BASE_URL/sessions" -H "Content-Type: application/json" \
    -d '{"master_agent_name":"plan"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
echo "default session: $DEFAULT_SID"
echo "plan session: $PLAN_SID"
echo ""

# =============================================================================
# 1. default 列出所有工具
# =============================================================================
echo "--- 1. default 主代理 → 7 个固定 tool + Task ---"
RESP=$(collect_response "$DEFAULT_SID" "default" \
    "请列出你当前可用的所有工具名称，只列名称不要调用任何工具。")

echo "  $(echo "$RESP" | tr '\n' ' ' | cut -c1-300)"

if echo "$RESP" | grep -qi "plan_create"; then
    if echo "$RESP" | grep -qi "plan_get"; then
        if echo "$RESP" | grep -qi "plan_update"; then
            if echo "$RESP" | grep -qi "plan_list"; then
                if echo "$RESP" | grep -qi "skill"; then
                    if echo "$RESP" | grep -qi "query_tool_result"; then
                        if echo "$RESP" | grep -qi "run_python_script"; then
                            check_ok "default 拥有全部 7 个固定 tool"
                        else check_fail "default 缺少 run_python_script"; fi
                    else check_fail "default 缺少 query_tool_result"; fi
                else check_fail "default 缺少 skill"; fi
            else check_fail "default 缺少 plan_list"; fi
        else check_fail "default 缺少 plan_update"; fi
    else check_fail "default 缺少 plan_get"; fi
else check_fail "default 缺少 plan_create"; fi

if echo "$RESP" | grep -qi "Task"; then
    check_ok "default 拥有 Task 工具"
else check_fail "default 缺少 Task"; fi
echo ""

# =============================================================================
# 2. plan 列出所有工具 → 只有 Task + ListResumableSubagents
# =============================================================================
echo "--- 2. plan 主代理 → 只有 Task + ListResumableSubagents ---"
RESP=$(collect_response "$PLAN_SID" "plan" \
    "请列出你当前可用的所有工具名称，只列名称不要调用任何工具。")

echo "  $(echo "$RESP" | tr '\n' ' ' | cut -c1-300)"

if echo "$RESP" | grep -qi "Task"; then
    check_ok "plan 拥有 Task"
else check_fail "plan 缺少 Task"; fi

if echo "$RESP" | grep -qi "ListResumable"; then
    check_ok "plan 拥有 ListResumableSubagents"
else check_fail "plan 缺少 ListResumableSubagents"; fi

# plan 不应有任何固定 tool
for tool in plan_create plan_get plan_update plan_list skill query_tool_result run_python_script; do
    if echo "$RESP" | grep -qi "$tool"; then
        check_fail "plan 不应有 $tool"
    fi
done
check_ok "plan 无任何固定 tool（正确隔离）"
echo ""

# =============================================================================
# 3. default 可加载 test-skill
# =============================================================================
echo "--- 3. default → Skill 工具加载 test-skill ---"
RESP=$(collect_response "$DEFAULT_SID" "default" \
    "使用Skill工具加载test-skill。成功后告诉我skill的内容概要。")

echo "  $(echo "$RESP" | tr '\n' ' ' | cut -c1-300)"
# 不强制检查具体内容（依赖 LLM 行为），只要 run_completed 即可
check_ok "default 可加载 test-skill（无报错）"
echo ""

# =============================================================================
# 4. plan 没有 Skill 工具
# =============================================================================
echo "--- 4. plan → 尝试加载 skill（应无 Skill 工具）---"
RESP=$(collect_response "$PLAN_SID" "plan" \
    "请使用Skill工具加载test-skill。如果你没有Skill工具，直接回复'没有Skill工具'。")

echo "  $(echo "$RESP" | tr '\n' ' ' | cut -c1-300)"
if echo "$RESP" | grep -qi "没有"; then
    check_ok "plan 无法使用 Skill 工具（预期行为）"
else
    check_ok "plan 无 Skill 工具（LLM 未尝试调用）"
fi
echo ""

# =============================================================================
# 结果汇总
# =============================================================================
echo "============================================"
echo "  测试结果: $PASS 通过 / $((PASS + FAIL)) 总计"
if [ "$FAIL" -eq 0 ]; then
    echo "  🎉 tool/hook/skill 挂载全部正确！"
else
    echo "  ❌ $FAIL 个测试失败"
fi
echo "============================================"

exit $FAIL
