#!/usr/bin/env bash
# =============================================================================
# Store Protocol 重构 - curl 冒烟测试
# =============================================================================
# 验证本次重构后所有 HTTP 端点正常运行，确保 StoreTransaction 和
# RunCancelBus 接入正确，session/run/chat/cancel 链路完整。
#
# 用法:
#   1. 先在一个终端启动服务: .venv/bin/python start.py
#   2. 在另一个终端运行本脚本: bash tests/curl_test/smoke_test_store_protocol.sh
# =============================================================================

set -euo pipefail

# 配置
BASE_URL="${1:-http://127.0.0.1:8000}"
PASS=0
FAIL=0
FAILURES=()

# 日志颜色
green()  { printf "\033[32m%s\033[0m\n" "$1"; }
red()    { printf "\033[31m%s\033[0m\n" "$1"; }
yellow() { printf "\033[33m%s\033[0m\n" "$1"; }

# 测试断言函数
assert_http_ok() {
    local desc="$1" http_code="$2"
    if [ "$http_code" -eq 200 ]; then
        green "  ✅ PASS: $desc"
        PASS=$((PASS + 1))
    else
        red "  ❌ FAIL: $desc (HTTP $http_code)"
        FAIL=$((FAIL + 1))
        FAILURES+=("$desc: HTTP $http_code")
    fi
}

assert_json_field() {
    local desc="$1" body="$2" field="$3" expected="$4"
    local actual
    actual=$(echo "$body" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('$field','__MISSING__'))" 2>/dev/null || echo "__PARSE_ERROR__")
    if [ "$actual" = "$expected" ]; then
        green "  ✅ PASS: $desc ($field=$actual)"
        PASS=$((PASS + 1))
    else
        red "  ❌ FAIL: $desc (expected $field='$expected', got '$actual')"
        FAIL=$((FAIL + 1))
        FAILURES+=("$desc: $field expected='$expected' actual='$actual'")
    fi
}

assert_json_field_exists() {
    local desc="$1" body="$2" field="$3"
    local actual
    actual=$(echo "$body" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('$field','__MISSING__'))" 2>/dev/null || echo "__PARSE_ERROR__")
    if [ "$actual" != "__MISSING__" ] && [ "$actual" != "__PARSE_ERROR__" ]; then
        green "  ✅ PASS: $desc ($field=$actual)"
        PASS=$((PASS + 1))
    else
        red "  ❌ FAIL: $desc ($field missing or parse error)"
        FAIL=$((FAIL + 1))
        FAILURES+=("$desc: $field missing")
    fi
}

# -----------------------------------------------------------------------------
echo "============================================================"
echo " Store Protocol 重构 - curl 冒烟测试"
echo " 目标: $BASE_URL"
echo " 时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================================"
echo ""

# ---- 1. 健康检查 ----
echo "--- 1. 健康检查 ---"

echo "  [GET /health]"
HTTP_CODE=$(curl -s -o /tmp/smoke_health.json -w "%{http_code}" "$BASE_URL/health")
assert_http_ok "GET /health 返回 200" "$HTTP_CODE"
BODY=$(cat /tmp/smoke_health.json)
assert_json_field "status 字段" "$BODY" "status" "ok"

echo "  [GET /health/ready]"
HTTP_CODE=$(curl -s -o /tmp/smoke_ready.json -w "%{http_code}" "$BASE_URL/health/ready")
assert_http_ok "GET /health/ready 返回 200" "$HTTP_CODE"
BODY=$(cat /tmp/smoke_ready.json)
assert_json_field "ready status 字段" "$BODY" "status" "ready"

echo "  [GET /health/live]"
HTTP_CODE=$(curl -s -o /tmp/smoke_live.json -w "%{http_code}" "$BASE_URL/health/live")
assert_http_ok "GET /health/live 返回 200" "$HTTP_CODE"
BODY=$(cat /tmp/smoke_live.json)
assert_json_field "live status 字段" "$BODY" "status" "alive"

echo ""

# ---- 2. 会话管理 ----
echo "--- 2. 会话管理 ---"

echo "  [POST /sessions]"
HTTP_CODE=$(curl -s -o /tmp/smoke_session.json -w "%{http_code}" \
    -X POST "$BASE_URL/sessions" \
    -H "Content-Type: application/json")
assert_http_ok "POST /sessions 返回 200" "$HTTP_CODE"

BODY=$(cat /tmp/smoke_session.json)
SESSION_ID=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])" 2>/dev/null || echo "")
assert_json_field_exists "session_id 已返回" "$BODY" "session_id"
assert_json_field_exists "agent_id 已返回" "$BODY" "agent_id"

if [ -z "$SESSION_ID" ]; then
    red "  ❌ 无法获取 session_id，后续测试将跳过"
    SESSION_ID="unknown"
fi

echo "  [GET /sessions/$SESSION_ID]"
HTTP_CODE=$(curl -s -o /tmp/smoke_get_session.json -w "%{http_code}" \
    "$BASE_URL/sessions/$SESSION_ID")
assert_http_ok "GET /sessions/{id} 返回 200" "$HTTP_CODE"
BODY=$(cat /tmp/smoke_get_session.json)
assert_json_field "session_id 匹配" "$BODY" "session_id" "$SESSION_ID"

echo "  [GET /sessions/nonexistent]"
HTTP_CODE=$(curl -s -o /tmp/smoke_session_404.json -w "%{http_code}" \
    "$BASE_URL/sessions/session-nonexistent-12345")
# 业务上返回 200 + request_failed
assert_http_ok "GET /sessions/不存在的ID 返回 200" "$HTTP_CODE"
BODY=$(cat /tmp/smoke_session_404.json)
assert_json_field "不存在的session返回request_failed" "$BODY" "type" "request_failed"

echo ""

# ---- 3. 聊天 (SSE) 与 Run 生命周期 ----
echo "--- 3. 聊天与 Run 生命周期（核心链路）---"

# 3a. 发送聊天请求，捕获 run_id
echo "  [POST /chat — 发送消息并捕获 run_id]"
HTTP_CODE=$(curl -s -o /tmp/smoke_chat_sse.txt -w "%{http_code}" \
    -X POST "$BASE_URL/chat" \
    -H "Content-Type: application/json" \
    -d "{\"session_id\": \"$SESSION_ID\", \"message\": \"请回复：冒烟测试通过\"}")
assert_http_ok "POST /chat 返回 200" "$HTTP_CODE"

# 从 SSE 流中提取 run_id
RUN_ID=$(python3 -c "
import sys, json
with open('/tmp/smoke_chat_sse.txt') as f:
    for line in f:
        if line.startswith('data: {'):
            try:
                d = json.loads(line[6:])
                if 'run_id' in d:
                    print(d['run_id'])
                    break
            except: pass
" 2>/dev/null || echo "")
if [ -n "$RUN_ID" ]; then
    green "  ✅ PASS: 已提取 run_id=$RUN_ID"
    PASS=$((PASS + 1))
else
    red "  ❌ FAIL: 未能从SSE中提取 run_id"
    FAIL=$((FAIL + 1))
    FAILURES+=("POST /chat: 未能提取 run_id")
fi

# 检查 SSE 关键事件
for event in "run_started" "message_delta" "run_completed"; do
    if grep -q "event: $event" /tmp/smoke_chat_sse.txt 2>/dev/null; then
        green "  ✅ PASS: SSE 包含 $event 事件"
        PASS=$((PASS + 1))
    else
        yellow "  ⚠️  WARN: SSE 未找到 $event 事件"
    fi
done

echo ""

# 3b. 查询 Run
if [ -n "$RUN_ID" ]; then
    echo "  [GET /runs/$RUN_ID]"
    HTTP_CODE=$(curl -s -o /tmp/smoke_get_run.json -w "%{http_code}" \
        "$BASE_URL/runs/$RUN_ID")
    assert_http_ok "GET /runs/{id} 返回 200" "$HTTP_CODE"
    BODY=$(cat /tmp/smoke_get_run.json)
    assert_json_field "run_id 匹配" "$BODY" "run_id" "$RUN_ID"

    echo "  [GET /runs/nonexistent]"
    HTTP_CODE=$(curl -s -o /tmp/smoke_run_404.json -w "%{http_code}" \
        "$BASE_URL/runs/run-nonexistent-12345")
    assert_http_ok "GET /runs/不存在的ID 返回 200" "$HTTP_CODE"
    BODY=$(cat /tmp/smoke_run_404.json)
    assert_json_field "不存在的run返回request_failed" "$BODY" "type" "request_failed"

    echo ""
else
    yellow "  ⚠️  跳过 Run 查询测试（无 run_id）"
fi

echo ""

# ---- 4. 取消 Run（需新建一个 run 来取消）----
echo "--- 4. 取消 Run ---"

# 创建一个新会话用于取消测试
echo "  [创建取消测试会话]"
HTTP_CODE=$(curl -s -o /tmp/smoke_cancel_session.json -w "%{http_code}" \
    -X POST "$BASE_URL/sessions" \
    -H "Content-Type: application/json")
CANCEL_SESSION_ID=$(python3 -c "import json; print(json.load(open('/tmp/smoke_cancel_session.json'))['session_id'])" 2>/dev/null || echo "")

# 发起一个聊天来创建 run
echo "  [发起聊天以创建可取消的 run]"
curl -s -o /tmp/smoke_cancel_chat.txt \
    -X POST "$BASE_URL/chat" \
    -H "Content-Type: application/json" \
    -d "{\"session_id\": \"$CANCEL_SESSION_ID\", \"message\": \"请回复一段长文本\"}" &

CHAT_PID=$!
sleep 1  # 等待 run 开始

CANCEL_RUN_ID=$(python3 -c "
import sys, json
try:
    with open('/tmp/smoke_cancel_chat.txt') as f:
        for line in f:
            if line.startswith('data: {'):
                try:
                    d = json.loads(line[6:])
                    if 'run_id' in d:
                        print(d['run_id'])
                        break
                except: pass
except: pass
" 2>/dev/null || echo "")

if [ -n "$CANCEL_RUN_ID" ]; then
    echo "  [POST /runs/$CANCEL_RUN_ID/cancel]"

    # 给取消操作一点时间以确保 run 处于 running 状态
    sleep 1

    HTTP_CODE=$(curl -s -o /tmp/smoke_cancel_result.json -w "%{http_code}" \
        -X POST "$BASE_URL/runs/$CANCEL_RUN_ID/cancel" \
        -H "Content-Type: application/json")
    assert_http_ok "POST /runs/{id}/cancel 返回 200" "$HTTP_CODE"
    BODY=$(cat /tmp/smoke_cancel_result.json)

    # 取消应该返回 cancelled=true 或者 request_failed（如果已经结束了）
    CANCELLED=$(echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('cancelled','__ABSENT__'))" 2>/dev/null || echo "")
    TYPE=$(echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('type','__ABSENT__'))" 2>/dev/null || echo "")

    if [ "$CANCELLED" = "True" ] || [ "$CANCELLED" = "true" ]; then
        green "  ✅ PASS: 取消成功 (cancelled=true)"
        PASS=$((PASS + 1))
    elif [ "$TYPE" = "request_failed" ]; then
        yellow "  ⚠️  INFO: 取消时 run 已结束（返回 request_failed，属正常竞态）"
        PASS=$((PASS + 1))
    else
        red "  ❌ FAIL: 取消结果异常 (BODY=$BODY)"
        FAIL=$((FAIL + 1))
        FAILURES+=("POST /runs/cancel: 异常响应")
    fi
else
    yellow "  ⚠️  跳过取消测试（未捕获到 run_id）"
fi

# 清理后台chat进程
wait $CHAT_PID 2>/dev/null || true

echo ""

# ---- 5. 多轮对话测试（验证 StoreTransaction 复合写入链路）----
echo "--- 5. 多轮对话（验证 StoreTransaction 复合写入）---"

echo "  [创建多轮测试会话]"
HTTP_CODE=$(curl -s -o /tmp/smoke_multiturn_session.json -w "%{http_code}" \
    -X POST "$BASE_URL/sessions" \
    -H "Content-Type: application/json")
MULTI_SESSION_ID=$(python3 -c "import json; print(json.load(open('/tmp/smoke_multiturn_session.json'))['session_id'])" 2>/dev/null || echo "")

echo "  [第1轮]"
curl -s -o /tmp/smoke_multiturn_1.txt \
    -X POST "$BASE_URL/chat" \
    -H "Content-Type: application/json" \
    -d "{\"session_id\": \"$MULTI_SESSION_ID\", \"message\": \"你好，我是测试\"}"
RUN1_ID=$(python3 -c "
import sys, json
with open('/tmp/smoke_multiturn_1.txt') as f:
    for line in f:
        if line.startswith('data: {'):
            try:
                d = json.loads(line[6:])
                if 'run_id' in d:
                    print(d['run_id'])
                    break
            except: pass
" 2>/dev/null || echo "")
if grep -q "event: run_completed" /tmp/smoke_multiturn_1.txt 2>/dev/null; then
    green "  ✅ PASS: 第1轮 run_completed"
    PASS=$((PASS + 1))
else
    yellow "  ⚠️  WARN: 第1轮未找到 run_completed"
fi

echo "  [第2轮 — 上下文连续性]"
curl -s -o /tmp/smoke_multiturn_2.txt \
    -X POST "$BASE_URL/chat" \
    -H "Content-Type: application/json" \
    -d "{\"session_id\": \"$MULTI_SESSION_ID\", \"message\": \"上一轮我说了什么？\"}"
if grep -q "event: run_completed" /tmp/smoke_multiturn_2.txt 2>/dev/null; then
    green "  ✅ PASS: 第2轮 run_completed（多轮上下文正常）"
    PASS=$((PASS + 1))
else
    yellow "  ⚠️  WARN: 第2轮未找到 run_completed"
fi

echo "  [验证会话消息数]"
HTTP_CODE=$(curl -s -o /tmp/smoke_multiturn_view.json -w "%{http_code}" \
    "$BASE_URL/sessions/$MULTI_SESSION_ID")
BODY=$(cat /tmp/smoke_multiturn_view.json)
MSG_COUNT=$(echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('message_count', -1))" 2>/dev/null || echo "-1")
if [ "$MSG_COUNT" -gt 0 ] 2>/dev/null; then
    green "  ✅ PASS: message_count=$MSG_COUNT > 0（上下文消息落库正常）"
    PASS=$((PASS + 1))
else
    red "  ❌ FAIL: message_count=$MSG_COUNT（上下文消息可能未正确落库）"
    FAIL=$((FAIL + 1))
    FAILURES+=("多轮对话: message_count异常: $MSG_COUNT")
fi

echo ""

# ---- 6. 请求验证 ----
echo "--- 6. 请求验证 ---"

echo "  [POST /chat 缺少必填字段]"
HTTP_CODE=$(curl -s -o /tmp/smoke_validation.json -w "%{http_code}" \
    -X POST "$BASE_URL/chat" \
    -H "Content-Type: application/json" \
    -d "{}")
# 预期 HTTP 422（FastAPI 参数校验）
if [ "$HTTP_CODE" -eq 422 ]; then
    green "  ✅ PASS: 缺少必填字段返回 422"
    PASS=$((PASS + 1))
else
    red "  ❌ FAIL: 缺少必填字段返回 $HTTP_CODE（预期 422）"
    FAIL=$((FAIL + 1))
    FAILURES+=("POST /chat 校验: HTTP $HTTP_CODE (预期 422)")
fi

echo ""

# ---- 汇总 ----
echo "============================================================"
echo "                    冒烟测试结果汇总"
echo "============================================================"
echo "  通过: $PASS"
echo "  失败: $FAIL"
echo ""

if [ "$FAIL" -gt 0 ]; then
    red "❌ 存在 $FAIL 个失败用例:"
    for f in "${FAILURES[@]}"; do
        red "    - $f"
    done
    echo ""
    red "冒烟测试不通过"
    exit 1
else
    green "✅ 全部冒烟测试通过"
    exit 0
fi
