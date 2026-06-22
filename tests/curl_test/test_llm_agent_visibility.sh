#!/bin/bash
# =============================================================================
# LLM 子代理可见性验证
# 通过 LLM 回复确认模型是否读取到正确的子代理列表
#
# 验证点：
#   1. default 主代理 → Task工具描述应列出 Worker, Echo, BothAgent
#   2. default 主代理 → 不应列出 PlanOnlyAgent
#   3. plan 主代理   → Task工具描述应列出 PlanOnlyAgent, BothAgent
#   4. plan 主代理   → 不应列出 Worker, Echo
# =============================================================================

set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
PASS=0
FAIL=0

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

collect_chat_response() {
    # 发起 SSE 聊天并收集 assistant 的最终回复文本
    local session_id="$1" master_agent="$2" message="$3"
    curl -s -N -X POST "$BASE_URL/chat" \
        -H "Content-Type: application/json" \
        -d "{\"session_id\":\"$session_id\",\"master_agent_name\":\"$master_agent\",\"message\":\"$message\"}" 2>&1 | \
        python3 "$SCRIPT_DIR/_collect_sse.py"
}

check_contains() {
    local label="$1" text="$2" word="$3" should_contain="$4"
    if echo "$text" | grep -qiF "$word"; then
        if [ "$should_contain" = "yes" ]; then
            echo "  ✅ $label (包含 '$word')"
            PASS=$((PASS + 1))
        else
            echo "  ❌ $label (不应包含 '$word' 但包含了)"
            FAIL=$((FAIL + 1))
        fi
    else
        if [ "$should_contain" = "yes" ]; then
            echo "  ❌ $label (应包含 '$word' 但未找到)"
            FAIL=$((FAIL + 1))
        else
            echo "  ✅ $label (不包含 '$word')"
            PASS=$((PASS + 1))
        fi
    fi
}

echo "============================================"
echo "  LLM 子代理可见性验证"
echo "  通过 LLM 回复确认 Task 工具描述"
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
# 1. 询问 default 主代理：列出 Task 工具中可用的子代理
# =============================================================================
echo "--- 1. default 主代理 → 询问可用的子代理类型 ---"
DEFAULT_RESP=$(collect_chat_response "$DEFAULT_SID" "default" \
    "请列出你的Task工具中所有可用的子代理类型名称。只列出名称，不要调用工具。")

echo "  LLM 回复摘要: $(echo "$DEFAULT_RESP" | head -1 | cut -c1-200)"

check_contains "default 可见 Worker"     "$DEFAULT_RESP" "Worker"        "yes"
check_contains "default 可见 Echo"       "$DEFAULT_RESP" "Echo"          "yes"
check_contains "default 可见 BothAgent"  "$DEFAULT_RESP" "BothAgent"     "yes"
check_contains "default 不可见 PlanOnly" "$DEFAULT_RESP" "PlanOnlyAgent" "no"
echo ""

# =============================================================================
# 2. 询问 plan 主代理：列出 Task 工具中可用的子代理
# =============================================================================
echo "--- 2. plan 主代理 → 询问可用的子代理类型 ---"
PLAN_RESP=$(collect_chat_response "$PLAN_SID" "plan" \
    "请列出你的Task工具中所有可用的子代理类型名称。只列出名称，不要调用工具。")

echo "  LLM 回复摘要: $(echo "$PLAN_RESP" | head -1 | cut -c1-200)"

check_contains "plan 可见 PlanOnlyAgent" "$PLAN_RESP" "PlanOnlyAgent" "yes"
check_contains "plan 可见 BothAgent"     "$PLAN_RESP" "BothAgent"     "yes"
check_contains "plan 不可见 Worker"      "$PLAN_RESP" "Worker"        "no"
check_contains "plan 不可见 Echo"        "$PLAN_RESP" "Echo"          "no"
echo ""

# =============================================================================
# 3. 验证 default 可以成功派发 Echo（实际调用）
# =============================================================================
echo "--- 3. default 主代理 → 实际派发 Echo ---"
ECHO_RESP=$(collect_chat_response "$DEFAULT_SID" "default" \
    "使用Task工具调用Echo子代理，subagent_type=Echo，description='echo test'，prompt='回复visible to default即可'")

check_contains "default→Echo 成功返回" "$ECHO_RESP" "visible to default" "yes"
echo ""

# =============================================================================
# 4. 验证 plan 可以成功派发 PlanOnlyAgent（实际调用）
# =============================================================================
echo "--- 4. plan 主代理 → 实际派发 PlanOnlyAgent ---"
PLAN_ONLY_RESP=$(collect_chat_response "$PLAN_SID" "plan" \
    "使用Task工具调用PlanOnlyAgent子代理，subagent_type=PlanOnlyAgent，description='mount test'，prompt='回复visible to plan only即可'")

check_contains "plan→PlanOnlyAgent 成功返回" "$PLAN_ONLY_RESP" "visible to plan only" "yes"
echo ""

# =============================================================================
# 结果汇总
# =============================================================================
echo "============================================"
echo "  测试结果: $PASS 通过 / $((PASS + FAIL)) 总计"
if [ "$FAIL" -eq 0 ]; then
    echo "  🎉 全部通过！LLM 正确读取了挂载配置"
else
    echo "  ❌ $FAIL 个测试失败"
fi
echo "============================================"

exit $FAIL
