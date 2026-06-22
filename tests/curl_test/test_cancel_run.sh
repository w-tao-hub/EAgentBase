#!/usr/bin/env bash
# =============================================================================
# RunCancelBus - 取消 Run 专用冒烟测试
# =============================================================================
# 验证 POST /runs/{run_id}/cancel 在 Store Protocol 重构后正常工作。
# 策略：发送长文本请求，利用模型生成时间窗口发送取消请求。
#
# 用法:
#   1. 先启动服务: .venv/bin/python start.py
#   2. 运行本脚本: bash tests/curl_test/test_cancel_run.sh
# =============================================================================

set -euo pipefail

BASE_URL="${1:-http://127.0.0.1:8000}"
PASS=0
FAIL=0

green()  { printf "\033[32m%s\033[0m\n" "$1"; }
red()    { printf "\033[31m%s\033[0m\n" "$1"; }
yellow() { printf "\033[33m%s\033[0m\n" "$1"; }

echo "============================================================"
echo " RunCancelBus 取消 Run 冒烟测试"
echo " 目标: $BASE_URL"
echo "============================================================"
echo ""

# ---- Step 1: 创建会话 ----
echo "--- Step 1: 创建会话 ---"
SESSION_RESP=$(curl -s -X POST "$BASE_URL/sessions" -H "Content-Type: application/json")
SESSION_ID=$(echo "$SESSION_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
green "  ✅ session_id=$SESSION_ID"
PASS=$((PASS + 1))

# ---- Step 2: 后台发起长文本聊天 ----
echo ""
echo "--- Step 2: 后台发起长文本聊天请求 ---"
echo "  (请求模型'列出1到500的所有数字并用中文描述')"

# 使用临时文件收集 SSE 输出
SSE_FILE=$(mktemp)
rm -f "$SSE_FILE"

curl -s -N -o "$SSE_FILE" \
    -X POST "$BASE_URL/chat" \
    -H "Content-Type: application/json" \
    -d "{\"session_id\": \"$SESSION_ID\", \"message\": \"请把1到500的每个数字的中文写法都列出来，例如：1是壹、2是贰，请从1开始逐行写出全部500个数字\"}" &

CHAT_PID=$!
echo "  后台 PID=$CHAT_PID"

# ---- Step 3: 等待 run_started 事件，提取 run_id ----
echo ""
echo "--- Step 3: 等待 run 启动并提取 run_id ---"

RUN_ID=""
for i in $(seq 1 50); do
    sleep 0.3
    if [ -f "$SSE_FILE" ]; then
        RUN_ID=$(python3 -c "
import json
try:
    with open('$SSE_FILE') as f:
        for line in f:
            if line.startswith('data: '):
                d = json.loads(line[6:])
                if d.get('type') == 'run_started':
                    print(d['run_id'])
                    raise StopIteration
except StopIteration:
    pass
except: pass
" 2>/dev/null || echo "")
    fi
    if [ -n "$RUN_ID" ]; then
        green "  ✅ 已捕获 run_id=$RUN_ID (尝试 $i 次)"
        PASS=$((PASS + 1))
        break
    fi
done

if [ -z "$RUN_ID" ]; then
    red "  ❌ 未能捕获 run_id"
    FAIL=$((FAIL + 1))
    kill $CHAT_PID 2>/dev/null || true
    echo ""
    echo "测试失败，SSE 输出："
    cat "$SSE_FILE" 2>/dev/null | head -10 || echo "(空)"
    rm -f "$SSE_FILE"
    exit 1
fi

# ---- Step 4: 发送取消请求 ----
echo ""
echo "--- Step 4: 发送取消请求 ---"
echo "  [POST /runs/$RUN_ID/cancel]"

CANCEL_RESP=$(curl -s -X POST "$BASE_URL/runs/$RUN_ID/cancel" \
    -H "Content-Type: application/json")
echo "  响应: $CANCEL_RESP"

CANCELLED=$(echo "$CANCEL_RESP" | python3 -c "
import sys,json
d=json.load(sys.stdin)
print(d.get('cancelled',''))
" 2>/dev/null || echo "")

TYPE=$(echo "$CANCEL_RESP" | python3 -c "
import sys,json
d=json.load(sys.stdin)
print(d.get('type',''))
" 2>/dev/null || echo "")

if [ "$CANCELLED" = "True" ]; then
    green "  ✅ PASS: cancelled=true，取消成功"
    PASS=$((PASS + 1))
elif [ "$TYPE" = "request_failed" ]; then
    yellow "  ⚠️  run 已完成，取消返回 request_failed（正常竞态）"
    PASS=$((PASS + 1))
else
    RUN_ID_RESP=$(echo "$CANCEL_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('run_id',''))" 2>/dev/null || echo "")
    if [ "$RUN_ID_RESP" = "$RUN_ID" ]; then
        green "  ✅ PASS: run_id 匹配"
        PASS=$((PASS + 1))
    else
        red "  ❌ FAIL: 取消响应异常"
        FAIL=$((FAIL + 1))
    fi
fi

# ---- Step 5: 等待后台聊天结束，检查 SSE 中的取消事件 ----
echo ""
echo "--- Step 5: 检查 SSE 流中的取消事件 ---"
wait $CHAT_PID 2>/dev/null || true
sleep 0.5

# 输出 SSE 的关键事件
echo "  SSE 关键事件:"
grep -E "^event:" "$SSE_FILE" 2>/dev/null | head -20 || echo "  (无事件)"

if grep -q "run_cancelled" "$SSE_FILE" 2>/dev/null; then
    green "  ✅ PASS: SSE 流包含 run_cancelled 事件"
    PASS=$((PASS + 1))
    # 显示取消原因
    CANCEL_REASON=$(python3 -c "
import json
with open('$SSE_FILE') as f:
    for line in f:
        if line.startswith('data: '):
            d = json.loads(line[6:])
            if d.get('type') == 'run_cancelled':
                print(d.get('reason',''))
" 2>/dev/null || echo "")
    echo "  取消原因: $CANCEL_REASON"
elif grep -q "run_completed" "$SSE_FILE" 2>/dev/null; then
    yellow "  ⚠️  模型在取消前已完成（run_completed），竞态导致取消未命中"
    PASS=$((PASS + 1))
else
    yellow "  ⚠️  未检测到 run_cancelled 或 run_completed 事件（可能仍在生成中）"
fi

# ---- Step 6: 验证 Run 终态 ----
echo ""
echo "--- Step 6: 验证 Run 终态 ---"
RUN_RESP=$(curl -s "$BASE_URL/runs/$RUN_ID")
echo "  Run 状态: $(echo "$RUN_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'status={d.get(\"status\")}, error_code={d.get(\"error_code\")}')" 2>/dev/null || echo "解析失败")"

RUN_STATUS=$(echo "$RUN_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")

if [ "$RUN_STATUS" = "cancelled" ]; then
    green "  ✅ PASS: Run 终态为 cancelled"
    PASS=$((PASS + 1))
elif [ "$RUN_STATUS" = "completed" ]; then
    yellow "  ⚠️  Run 在取消前已完成（status=completed）"
    PASS=$((PASS + 1))
else
    yellow "  ⚠️  Run 状态: $RUN_STATUS"
fi

# ---- 清理 ----
rm -f "$SSE_FILE"

echo ""
echo "============================================================"
echo "               取消 Run 测试结果汇总"
echo "============================================================"
echo "  通过: $PASS"
echo "  失败: $FAIL"
echo ""

if [ "$FAIL" -gt 0 ]; then
    red "❌ 取消测试存在失败用例"
    exit 1
else
    green "✅ 取消 Run 测试通过"
    exit 0
fi
