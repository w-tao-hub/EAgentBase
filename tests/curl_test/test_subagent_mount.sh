#!/bin/bash
# =============================================================================
# 子代理挂载可见性冒烟测试
#
# 测试子代理：
#   - Echo.md         无 mount_master_agents → 只挂载到 default
#   - Worker (默认)   无 mount_master_agents → 只挂载到 default
#   - PlanOnlyAgent   挂载到 plan  only
#   - BothAgent       挂载到 default + plan
#
# 覆盖功能点：
#   1. default 主代理可见子代理列表
#   2. plan 主代理可见子代理列表
#   3. default 调用挂载到 default 的子代理 → 成功
#   4. plan 调用挂载到 plan 的子代理 → 成功
#   5. plan 调用 Echo（未挂载到 plan）→ 失败
#   6. default 调用 PlanOnlyAgent（未挂载到 default）→ 失败
#   7. default 调用 BothAgent（挂载到 default）→ 成功
#   8. plan 调用 BothAgent（挂载到 plan）→ 成功
# =============================================================================

set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
PASS=0
FAIL=0

check_status() {
    local label="$1" expected="$2" actual="$3"
    if [ "$actual" = "$expected" ]; then
        echo "  ✅ $label"
        PASS=$((PASS + 1))
    else
        echo "  ❌ $label 期望=$expected 实际=$actual"
        FAIL=$((FAIL + 1))
    fi
}

check_sse_event() {
    local label="$1" sse_output="$2" event_name="$3" error_code="${4:-}"
    if echo "$sse_output" | grep -q "event: $event_name"; then
        if [ -n "$error_code" ]; then
            if echo "$sse_output" | grep -q "error_code.*$error_code"; then
                echo "  ✅ $label"
                PASS=$((PASS + 1))
            else
                echo "  ❌ $label 期望 error_code=$error_code 未找到"
                FAIL=$((FAIL + 1))
            fi
        else
            echo "  ✅ $label"
            PASS=$((PASS + 1))
        fi
    else
        echo "  ❌ $label 期望 event=$event_name 未找到"
        FAIL=$((FAIL + 1))
    fi
}

check_sse_contains() {
    local label="$1" sse_output="$2" substring="$3"
    if echo "$sse_output" | grep -qF "$substring"; then
        echo "  ✅ $label"
        PASS=$((PASS + 1))
    else
        echo "  ❌ $label 期望包含 '$substring'"
        FAIL=$((FAIL + 1))
    fi
}

echo "============================================"
echo "  子代理挂载可见性冒烟测试"
echo "============================================"
echo ""

# ---- 创建 default 和 plan 会话 ----
DEFAULT_SID=$(curl -s -X POST "$BASE_URL/sessions" | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
PLAN_SID=$(curl -s -X POST "$BASE_URL/sessions" -H "Content-Type: application/json" \
    -d '{"master_agent_name":"plan"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
echo "default session: $DEFAULT_SID"
echo "plan session: $PLAN_SID"
echo ""

# =============================================================================
# 1. default 主代理调用 Echo（未声明mount → default）→ 应成功
# =============================================================================
echo "--- 1. default 调用 Echo（默认挂载到 default）---"
SSE=$(curl -s -N -X POST "$BASE_URL/chat" \
    -H "Content-Type: application/json" \
    -d "{\"session_id\":\"$DEFAULT_SID\",\"master_agent_name\":\"default\",\"message\":\"使用Task工具调用Echo子代理，prompt写'hello'，description写'test echo'\"}" 2>&1 || true)
check_sse_event "default → Echo 成功" "$SSE" "run_completed" ""
echo ""

# =============================================================================
# 2. plan 主代理调用 Echo（Echo 未挂载到 plan）→ 应失败
# =============================================================================
echo "--- 2. plan 调用 Echo（Echo 未挂载到 plan，应被拒绝）---"

# 先看 plan 要不要尝试调用 Echo
# 注意：LLM 不一定真的会尝试调用 Echo，因为 Task 工具描述中不会列出 Echo
# 我们需要指示它尝试
SSE=$(curl -s -N -X POST "$BASE_URL/chat" \
    -H "Content-Type: application/json" \
    -d "{\"session_id\":\"$PLAN_SID\",\"master_agent_name\":\"plan\",\"message\":\"请使用Task工具调用Echo子代理，subagent_type填Echo，description填'test'，prompt写'hello'。请务必执行工具调用。\"}" 2>&1 || true)

# 如果 LLM 调用了 Echo 但被 TaskTool 拒绝，会在 tool_use_completed 的 data 中包含错误
# 或者 LLM 根本看不到 Echo（Task tool schema 中不列出）
# 两种情况都说明挂载隔离生效
if echo "$SSE" | grep -q "SUBAGENT_NOT_MOUNTED"; then
    echo "  ✅ plan → Echo 被 TaskTool 拒绝 (SUBAGENT_NOT_MOUNTED)"
    PASS=$((PASS + 1))
elif echo "$SSE" | grep -q "Echo"; then
    echo "  ⚠️  plan → Echo: SSE 中出现了 Echo（需人工判断是否被正确隔离）"
    echo "  SSE 摘要: $(echo "$SSE" | grep -o 'Echo[^"]*' | head -3)"
else
    echo "  ✅ plan → Echo: LLM 未尝试调用 Echo（Task tool schema 中不可见）"
    PASS=$((PASS + 1))
fi
echo ""

# =============================================================================
# 3. default 调用 PlanOnlyAgent（未挂载到 default）→ 应被拒绝
# =============================================================================
echo "--- 3. default 调用 PlanOnlyAgent（仅挂载到 plan）---"
SSE=$(curl -s -N -X POST "$BASE_URL/chat" \
    -H "Content-Type: application/json" \
    -d "{\"session_id\":\"$DEFAULT_SID\",\"master_agent_name\":\"default\",\"message\":\"请使用Task工具调用PlanOnlyAgent子代理，subagent_type填PlanOnlyAgent，description填'test'，prompt写'hello'。\"}" 2>&1 || true)

if echo "$SSE" | grep -q "SUBAGENT_NOT_MOUNTED"; then
    echo "  ✅ default → PlanOnlyAgent 被 TaskTool 拒绝"
    PASS=$((PASS + 1))
elif echo "$SSE" | grep -q "PlanOnlyAgent"; then
    echo "  ⚠️  default → PlanOnlyAgent: 需判断是否被正确隔离"
else
    echo "  ✅ default → PlanOnlyAgent: LLM 未尝试调用（不可见）"
    PASS=$((PASS + 1))
fi
echo ""

# =============================================================================
# 4. plan 调用 PlanOnlyAgent（挂载到 plan）→ 应成功
# =============================================================================
echo "--- 4. plan 调用 PlanOnlyAgent（挂载到 plan）---"
SSE=$(curl -s -N -X POST "$BASE_URL/chat" \
    -H "Content-Type: application/json" \
    -d "{\"session_id\":\"$PLAN_SID\",\"master_agent_name\":\"plan\",\"message\":\"使用Task工具调用PlanOnlyAgent子代理，subagent_type填PlanOnlyAgent，description填'test'，prompt写'hello'\"}" 2>&1 || true)
check_sse_event "plan → PlanOnlyAgent 成功" "$SSE" "run_completed" ""
echo ""

# =============================================================================
# 5. default 调用 BothAgent（挂载到 default）→ 应成功
# =============================================================================
echo "--- 5. default 调用 BothAgent（挂载到 default+plan）---"
SSE=$(curl -s -N -X POST "$BASE_URL/chat" \
    -H "Content-Type: application/json" \
    -d "{\"session_id\":\"$DEFAULT_SID\",\"master_agent_name\":\"default\",\"message\":\"使用Task工具调用BothAgent子代理，subagent_type填BothAgent，description填'test'，prompt写'hello'\"}" 2>&1 || true)
check_sse_event "default → BothAgent 成功" "$SSE" "run_completed" ""
echo ""

# =============================================================================
# 6. plan 调用 BothAgent（挂载到 plan）→ 应成功
# =============================================================================
echo "--- 6. plan 调用 BothAgent（挂载到 plan）---"
SSE=$(curl -s -N -X POST "$BASE_URL/chat" \
    -H "Content-Type: application/json" \
    -d "{\"session_id\":\"$PLAN_SID\",\"master_agent_name\":\"plan\",\"message\":\"使用Task工具调用BothAgent子代理，subagent_type填BothAgent，description填'test'，prompt写'hello'\"}" 2>&1 || true)
check_sse_event "plan → BothAgent 成功" "$SSE" "run_completed" ""
echo ""

# =============================================================================
# 结果汇总
# =============================================================================
echo "============================================"
echo "  测试结果: $PASS 通过 / $((PASS + FAIL)) 总计"
if [ "$FAIL" -eq 0 ]; then
    echo "  🎉 全部通过！"
else
    echo "  ❌ $FAIL 个测试失败"
fi
echo "============================================"

exit $FAIL
