#!/bin/bash
# =============================================================================
# 多主代理路由冒烟测试
# 覆盖功能点：
#   1. POST /sessions 缺省创建 default 会话
#   2. POST /sessions 绑定 plan 主代理
#   3. POST /sessions 未知主代理 → 业务错误
#   4. POST /chat 缺少 master_agent_name → 422
#   5. POST /chat 显式路由到 default 主代理
#   6. POST /chat 显式路由到 plan 主代理
#   7. POST /chat 未知主代理 → request_failed
#   8. POST /chat session 主代理不匹配 → request_failed
#   9. GET /sessions/{id} 验证 agent_id 绑定
# =============================================================================

set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
PASS=0
FAIL=0

# ---- 辅助函数 ----
check_status() {
    local label="$1" expected="$2" actual="$3"
    if [ "$actual" = "$expected" ]; then
        echo "  ✅ $label (status=$actual)"
        PASS=$((PASS + 1))
    else
        echo "  ❌ $label 期望 status=$expected 实际=$actual"
        FAIL=$((FAIL + 1))
    fi
}

check_json_field() {
    local label="$1" json="$2" field="$3" expected="$4"
    local actual
    actual=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('$field',''))" 2>/dev/null || echo "")
    if [ "$actual" = "$expected" ]; then
        echo "  ✅ $label ($field=$actual)"
        PASS=$((PASS + 1))
    else
        echo "  ❌ $label 期望 $field='$expected' 实际='$actual'"
        FAIL=$((FAIL + 1))
    fi
}

check_contains() {
    local label="$1" json="$2" field="$3" substring="$4"
    local actual
    actual=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('$field',''))" 2>/dev/null || echo "")
    if echo "$actual" | grep -qF "$substring"; then
        echo "  ✅ $label ($field 包含 '$substring')"
        PASS=$((PASS + 1))
    else
        echo "  ❌ $label 期望 $field 包含 '$substring' 实际='$actual'"
        FAIL=$((FAIL + 1))
    fi
}

check_sse_event() {
    local label="$1" sse_output="$2" event_name="$3" error_code="${4:-}"
    if echo "$sse_output" | grep -q "event: $event_name"; then
        if [ -n "$error_code" ]; then
            if echo "$sse_output" | grep -q "error_code.*$error_code"; then
                echo "  ✅ $label (event=$event_name, error_code=$error_code)"
                PASS=$((PASS + 1))
            else
                echo "  ❌ $label 期望 error_code=$error_code 但未找到"
                FAIL=$((FAIL + 1))
            fi
        else
            echo "  ✅ $label (event=$event_name)"
            PASS=$((PASS + 1))
        fi
    else
        echo "  ❌ $label 期望 event=$event_name 但未找到"
        FAIL=$((FAIL + 1))
    fi
}

echo "============================================"
echo "  多主代理路由冒烟测试"
echo "  BASE_URL=$BASE_URL"
echo "============================================"
echo ""

# =============================================================================
# 1. POST /sessions — 缺省创建 default 会话（无请求体，向后兼容）
# =============================================================================
echo "--- 1. POST /sessions 缺省创建 default 会话 ---"
RESP=$(curl -s -w "\n%{http_code}" -X POST "$BASE_URL/sessions" \
    -H "Content-Type: application/json")
HTTP_CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | sed '$d')

check_status "HTTP 200" "200" "$HTTP_CODE"
check_json_field "agent_id=default" "$BODY" "agent_id" "default"
DEFAULT_SESSION_ID=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
echo "  session_id=$DEFAULT_SESSION_ID"
echo ""

# =============================================================================
# 2. POST /sessions — 绑定 plan 主代理
# =============================================================================
echo "--- 2. POST /sessions 绑定 plan 主代理 ---"
RESP=$(curl -s -w "\n%{http_code}" -X POST "$BASE_URL/sessions" \
    -H "Content-Type: application/json" \
    -d '{"master_agent_name": "plan"}')
HTTP_CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | sed '$d')

check_status "HTTP 200" "200" "$HTTP_CODE"
check_json_field "agent_id=plan" "$BODY" "agent_id" "plan"
PLAN_SESSION_ID=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
echo "  session_id=$PLAN_SESSION_ID"
echo ""

# =============================================================================
# 3. POST /sessions — 未知主代理 → 业务错误
# =============================================================================
echo "--- 3. POST /sessions 未知主代理 → 业务错误 ---"
RESP=$(curl -s -w "\n%{http_code}" -X POST "$BASE_URL/sessions" \
    -H "Content-Type: application/json" \
    -d '{"master_agent_name": "ghost"}')
HTTP_CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | sed '$d')

check_status "HTTP 200 (业务错误走 200)" "200" "$HTTP_CODE"
check_json_field "error_code=UNKNOWN_MASTER_AGENT" "$BODY" "error_code" "UNKNOWN_MASTER_AGENT"
echo ""

# =============================================================================
# 4. POST /chat — 缺少 master_agent_name → 422
# =============================================================================
echo "--- 4. POST /chat 缺少 master_agent_name → 422 ---"
RESP=$(curl -s -w "\n%{http_code}" -X POST "$BASE_URL/chat" \
    -H "Content-Type: application/json" \
    -d '{"session_id": "dummy", "message": "hi"}')
HTTP_CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | sed '$d')

check_status "HTTP 422" "422" "$HTTP_CODE"
check_contains "错误信息包含 master_agent_name" "$BODY" "message" "master_agent_name"
echo ""

# =============================================================================
# 5. POST /chat — 显式路由到 default 主代理（SSE）
# =============================================================================
echo "--- 5. POST /chat 显式路由到 default 主代理 ---"
SSE_OUTPUT=$(curl -s -N -X POST "$BASE_URL/chat" \
    -H "Content-Type: application/json" \
    -d "{\"session_id\": \"$DEFAULT_SESSION_ID\", \"master_agent_name\": \"default\", \"message\": \"回复OK即可\"}" 2>&1 || true)

check_sse_event "run_started" "$SSE_OUTPUT" "run_started" ""
check_sse_event "run_completed" "$SSE_OUTPUT" "run_completed" ""
echo ""

# =============================================================================
# 6. POST /chat — 显式路由到 plan 主代理（SSE）
# =============================================================================
echo "--- 6. POST /chat 显式路由到 plan 主代理 ---"
SSE_OUTPUT=$(curl -s -N -X POST "$BASE_URL/chat" \
    -H "Content-Type: application/json" \
    -d "{\"session_id\": \"$PLAN_SESSION_ID\", \"master_agent_name\": \"plan\", \"message\": \"回复OK即可\"}" 2>&1 || true)

check_sse_event "run_started" "$SSE_OUTPUT" "run_started" ""
check_sse_event "run_completed" "$SSE_OUTPUT" "run_completed" ""
echo ""

# =============================================================================
# 7. POST /chat — 未知主代理 → request_failed
# =============================================================================
echo "--- 7. POST /chat 未知主代理 → request_failed ---"
SSE_OUTPUT=$(curl -s -N -X POST "$BASE_URL/chat" \
    -H "Content-Type: application/json" \
    -d "{\"session_id\": \"$DEFAULT_SESSION_ID\", \"master_agent_name\": \"ghost\", \"message\": \"hi\"}" 2>&1 || true)

check_sse_event "request_failed (UNKNOWN_MASTER_AGENT)" "$SSE_OUTPUT" "request_failed" "UNKNOWN_MASTER_AGENT"
echo ""

# =============================================================================
# 8. POST /chat — session 主代理不匹配 → request_failed
# =============================================================================
echo "--- 8. POST /chat session 主代理不匹配 ---"
SSE_OUTPUT=$(curl -s -N -X POST "$BASE_URL/chat" \
    -H "Content-Type: application/json" \
    -d "{\"session_id\": \"$DEFAULT_SESSION_ID\", \"master_agent_name\": \"plan\", \"message\": \"hi\"}" 2>&1 || true)

check_sse_event "request_failed (MASTER_AGENT_SESSION_MISMATCH)" "$SSE_OUTPUT" "request_failed" "MASTER_AGENT_SESSION_MISMATCH"
echo ""

# =============================================================================
# 9. GET /sessions/{id} — 验证 agent_id 绑定
# =============================================================================
echo "--- 9. GET /sessions/{id} 验证 agent_id 绑定 ---"
RESP=$(curl -s -w "\n%{http_code}" -X GET "$BASE_URL/sessions/$DEFAULT_SESSION_ID")
HTTP_CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | sed '$d')
check_status "default session HTTP 200" "200" "$HTTP_CODE"
check_json_field "default session agent_id=default" "$BODY" "agent_id" "default"

RESP=$(curl -s -w "\n%{http_code}" -X GET "$BASE_URL/sessions/$PLAN_SESSION_ID")
HTTP_CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | sed '$d')
check_status "plan session HTTP 200" "200" "$HTTP_CODE"
check_json_field "plan session agent_id=plan" "$BODY" "agent_id" "plan"
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
