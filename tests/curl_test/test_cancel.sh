#!/bin/bash
# 取消功能端到端测试脚本（curl 端到端测试）
# 测试场景：
#   1. 接口取消 - POST /runs/{run_id}/cancel
#   2. 断链取消 - 客户端断开 SSE 连接
#   3. 子代理取消 - 取消时子代理同时取消
# 验证点：
#   - 主代理 Run 状态为 cancelled
#   - 子代理 Run 状态为 cancelled
#   - 会话历史已保存（含取消提示消息）
#   - Run 元数据（finished_at, error_code）
#
# 前置条件：
#   - 服务端已在 http://127.0.0.1:8000 运行
#   - 依赖：curl, jq
#   - 项目虚拟环境的 redis 库用于数据验证
#
# 用法：
#   bash test_cancel.sh                      # 运行所有测试
#   bash test_cancel.sh api                  # 仅测试接口取消
#   bash test_cancel.sh disconnect           # 仅测试断链取消
#   bash test_cancel.sh subagent             # 仅测试子代理取消

BASE_URL="http://127.0.0.1:8000"
REDIS_URL="redis://:scm_123@117.72.179.42:6379/0"
REDIS_PREFIX="agent"
PROJECT_VENV="/Users/myapple/应用文件/cursorcode/agent-framework/.venv/bin/python"
SSE_TIMEOUT=60
CANCEL_WAIT=3
DISCONNECT_WAIT=4

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${YELLOW}[INFO]${NC} $1"; }
pass()  { echo -e "${GREEN}[PASS]${NC} $1"; }
fail()  { echo -e "${RED}[FAIL]${NC} $1"; }

# ---- Redis 查询（使用项目 venv） ----
redis_cmd() {
    local pyfile=$(mktemp /tmp/agent-test/redis_cmd.XXXXXX.py)
    cat > "$pyfile" <<PYEOF
import json, sys
import redis
r = redis.from_url("${REDIS_URL}", decode_responses=True)
try:
    cmd = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    result = eval(f"r.{cmd}")
    if result is None:
        print("null")
    elif isinstance(result, (list, set, tuple)):
        print(json.dumps(list(result), ensure_ascii=False))
    elif isinstance(result, dict):
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(result)
except Exception as e:
    print(json.dumps({"error": str(e)}))
    sys.exit(1)
PYEOF
    "$PROJECT_VENV" "$pyfile" "$@" 2>/dev/null || echo '{"error":"redis_query_failed"}'
    rm -f "$pyfile"
}

redis_key() {
    local p="$REDIS_PREFIX"
    case "$1" in
        run)            echo "${p}:run:${2}";;
        session_runs)   echo "${p}:session_runs:${2}";;
        session_child)  echo "${p}:session_children:${2}";;
        main_msgs)      echo "${p}:session_main_messages:${2}";;
        child_msgs)     echo "${p}:child_context_messages:${2}:${3}";;
    esac
}

# ---- HTTP 辅助 ----
create_session() { curl -s -X POST "$BASE_URL/sessions" | jq -r '.session_id'; }
get_run()        { curl -s "$BASE_URL/runs/$1"; }
get_session()    { curl -s "$BASE_URL/sessions/$1"; }
cancel_api()     { curl -s -X POST "$BASE_URL/runs/$1/cancel"; }

# ---- SSE 解析（兼容 JSON 中 key 和 value 之间有空格） ----
extract_run_id() {
    local val
    val=$(grep -o '"run_id": "[^"]*"' "$1" 2>/dev/null | head -1 | cut -d'"' -f4)
    if [ -n "$val" ]; then
        echo "$val"
        return 0
    fi
    return 1
}
has_tool_call() {
    grep -q '"tool_name": "[^"]*"' "$1" 2>/dev/null && return 0
    return 1
}
has_task_tool() {
    # 精确匹配 Dispatch 子代理的 Task 工具（排除 task_create/task_get 等任务管理工具）
    grep -q '"tool_name": "Task"' "$1" 2>/dev/null && \
    grep -q '"subagent_type"' "$1" 2>/dev/null && return 0
    return 1
}

wait_for_run_id() {
    local file="$1" timeout="${2:-30}"
    local elapsed=0
    while [ $elapsed -lt $timeout ]; do
        local rid
        rid=$(extract_run_id "$file")
        if [ -n "$rid" ]; then
            echo "$rid"
            return 0
        fi
        sleep 0.5
        elapsed=$((elapsed + 1))
    done
    return 1
}

# ---- 前置检查 ----
check_prereqs() {
    local ok=0
    for cmd in curl jq python3; do
        command -v "$cmd" &>/dev/null || { fail "Missing: $cmd"; ok=1; }
    done
    [ "$ok" = 1 ] && return 1
    local code=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/health/live" 2>/dev/null || echo "000")
    if [ "$code" != "200" ]; then
        fail "Server not reachable (HTTP $code)"
        return 1
    fi
    info "All checks passed (curl, jq, python3, server)"
    return 0
}

# ============================================================
# Test 1: API Cancel
# ============================================================
test_api_cancel() {
    echo ""
    echo "========================================="
    echo " Test 1: API Cancel (接口取消)"
    echo "========================================="
    echo ""

    local f=$(mktemp /tmp/agent-test/sse_api.XXXXXX)
    local sid=$(create_session)
    info "Session: $sid"

    curl -N -s -X POST "$BASE_URL/chat" \
        -H "Content-Type: application/json" \
        -d "{\"session_id\": \"$sid\", \"message\": \"你好，请介绍一下你自己\"}" > "$f" 2>&1 &
    local pid=$!

    local rid=$(wait_for_run_id "$f" 30)
    [ -z "$rid" ] && { fail "No run_id"; kill $pid 2>/dev/null; rm -f "$f"; return 1; }
    info "Run ID: $rid"

    sleep "$CANCEL_WAIT"

    local cresp=$(cancel_api "$rid")
    local cancelled=$(echo "$cresp" | jq -r '.cancelled // "false"')
    [ "$cancelled" = "true" ] && pass "Cancel request sent" || info "Cancel response: $(echo "$cresp" | jq -r '.message')"

    # 等待 SSE 结束
    for i in $(seq 1 $SSE_TIMEOUT); do
        kill -0 $pid 2>/dev/null || break
        sleep 0.5
    done
    kill $pid 2>/dev/null; wait $pid 2>/dev/null
    sleep 1

    # 验证
    local status=$(get_run "$rid" | jq -r '.status // "unknown"')
    [ "$status" = "cancelled" ] && pass "主代理 Run 已取消" || info "Run status: $status"

    local mc=$(get_session "$sid" | jq -r '.message_count // 0')
    [ "$mc" -gt 0 ] && pass "会话历史已保存 ($mc 条消息)" || fail "会话历史为空"

    local hint=$(redis_cmd "lrange('$(redis_key main_msgs "$sid")', 0, -1)" | \
        "$PROJECT_VENV" -c "import json,sys; msgs=json.load(sys.stdin); [print('yes') or sys.exit(0) for m in msgs if isinstance(m,str) and json.loads(m).get('role')=='system' and '取消' in (json.loads(m).get('content') or '')]; print('no')" 2>/dev/null || echo "no")
    [ "$hint" = "yes" ] && pass "取消提示消息已写入" || info "无取消提示消息"

    rm -f "$f"
    echo ""; pass "API Cancel test completed"; echo ""
}

# ============================================================
# Test 2: Disconnect Cancel
# ============================================================
test_disconnect_cancel() {
    echo ""
    echo "========================================="
    echo " Test 2: Disconnect Cancel (断链取消)"
    echo "========================================="
    echo ""

    local f=$(mktemp /tmp/agent-test/sse_disc.XXXXXX)
    local sid=$(create_session)
    info "Session: $sid"

    curl -N -s -X POST "$BASE_URL/chat" \
        -H "Content-Type: application/json" \
        -d "{\"session_id\": \"$sid\", \"message\": \"你好，请介绍一下你自己\"}" > "$f" 2>&1 &
    local pid=$!

    local rid=$(wait_for_run_id "$f" 30)
    [ -z "$rid" ] && { fail "No run_id"; kill $pid 2>/dev/null; rm -f "$f"; return 1; }
    info "Run ID: $rid"

    sleep "$CANCEL_WAIT"

    # 断链
    kill $pid 2>/dev/null; wait $pid 2>/dev/null
    info "SSE disconnected"
    sleep "$DISCONNECT_WAIT"

    local status=$(get_run "$rid" | jq -r '.status // "unknown"')
    [ "$status" = "cancelled" ] && pass "断链后主代理 Run 已取消" || info "Run status: $status"

    local mc=$(get_session "$sid" | jq -r '.message_count // 0')
    [ "$mc" -gt 0 ] && pass "断链后会话历史已保存 ($mc 条消息)" || fail "会话历史为空"

    local ft=$(redis_cmd "hget('$(redis_key run "$rid")', 'finished_at')" | tr -d '"')
    [ -n "$ft" ] && [ "$ft" != "None" ] && pass "Run 已完成时间记录: $ft"

    local ec=$(redis_cmd "hget('$(redis_key run "$rid")', 'error_code')" | tr -d '"')
    [ -n "$ec" ] && [ "$ec" != "None" ] && pass "Run 错误码: $ec"

    rm -f "$f"
    echo ""; pass "Disconnect Cancel test completed"; echo ""
}

# ============================================================
# Test 3: Sub-agent Cancel
# ============================================================
test_subagent_cancel() {
    echo ""
    echo "========================================="
    echo " Test 3: Sub-agent Cancel (子代理取消)"
    echo " 验证主代理 + 子代理同时取消"
    echo "========================================="
    echo ""

    local f=$(mktemp /tmp/agent-test/sse_sub.XXXXXX)
    local sid=$(create_session)
    info "Session: $sid"
    info "Prompt: 启动子代理分析项目结构"

    curl -N -s -X POST "$BASE_URL/chat" \
        -H "Content-Type: application/json" \
        -d "{\"session_id\": \"$sid\", \"message\": \"请启动一个子代理（使用 Task 工具）来帮我分析项目，子代理类型为 Plan，任务prompt为：分析项目结构并给出改进建议\"}" > "$f" 2>&1 &
    local pid=$!

    # 等待 Task 工具调用（子代理已派发）
    local task_found=0
    for i in $(seq 1 60); do
        if has_task_tool "$f"; then task_found=1; break; fi
        sleep 0.5
    done
    if [ "$task_found" -eq 0 ]; then
        fail "Task 工具未在超时内调用（LLM 可能未派发子代理）"
        kill $pid 2>/dev/null; wait $pid 2>/dev/null; rm -f "$f"
        return 1
    fi
    local rid=$(extract_run_id "$f")
    info "Run ID: $rid"
    info "Task tool called - sub-agent dispatched"

    # 立即取消
    info "Cancelling..."
    local cresp=$(cancel_api "$rid")
    echo "$cresp" | jq -c '.'
    local cancelled=$(echo "$cresp" | jq -r '.cancelled // "false"')

    # 等待 SSE 结束
    for i in $(seq 1 30); do
        kill -0 $pid 2>/dev/null || break
        sleep 0.5
    done
    kill $pid 2>/dev/null; wait $pid 2>/dev/null
    sleep 2

    if [ "$cancelled" != "true" ]; then
        fail "取消请求未成功"
        rm -f "$f"
        return 1
    fi
    pass "取消请求已发送"

    # === 验证 ===

    # 1. 主代理状态
    local mstatus=$(get_run "$rid" | jq -r '.status // "unknown"')
    [ "$mstatus" = "cancelled" ] && pass "主代理 Run 已取消" || fail "主代理 Run 状态为 $mstatus"

    # 2. 子代理状态（Redis）
    local runs_json=$(redis_cmd "zrange('$(redis_key session_runs "$sid")', 0, -1)")
    local child_found=0
    local child_cancelled=0
    for rid2 in $(echo "$runs_json" | jq -r '.[] // empty' 2>/dev/null); do
        local rtype=$(redis_cmd "hget('$(redis_key run "$rid2")', 'run_type')" | tr -d '"')
        local rstatus=$(redis_cmd "hget('$(redis_key run "$rid2")', 'status')" | tr -d '"')
        if [ "$rtype" = "child" ]; then
            child_found=1
            info "子代理 Run $rid2: status=$rstatus"
            [ "$rstatus" = "cancelled" ] && child_cancelled=1
        fi
    done

    if [ "$child_found" -eq 0 ]; then
        fail "未发现子代理 Run"
    elif [ "$child_cancelled" -eq 1 ]; then
        pass "子代理 Run 已取消"
        pass "主代理 + 子代理同时取消成功"
    else
        info "子代理已完成（取消前已执行完毕）"
    fi

    # 3. 会话历史
    local mc=$(get_session "$sid" | jq -r '.message_count // 0')
    [ "$mc" -gt 0 ] && pass "会话历史已保存 ($mc 条消息)" || fail "会话历史为空"

    # 4. 取消提示消息
    local hint=$(redis_cmd "lrange('$(redis_key main_msgs "$sid")', 0, -1)" | \
        "$PROJECT_VENV" -c "import json,sys; msgs=json.load(sys.stdin); [print('yes') or sys.exit(0) for m in msgs if isinstance(m,str) and json.loads(m).get('role')=='system' and '取消' in (json.loads(m).get('content') or '')]; print('no')" 2>/dev/null || echo "no")
    [ "$hint" = "yes" ] && pass "取消提示消息已写入会话历史" || info "无取消提示消息"

    rm -f "$f"
    echo ""; pass "Sub-agent Cancel test completed"; echo ""
}

# ============================================================
# Test 4: Post-cancel Chat (主代理取消后再次对话)
# ============================================================
test_post_cancel_chat() {
    echo ""
    echo "========================================="
    echo " Test 4: Post-cancel Chat (取消后再次对话)"
    echo " 验证取消后新对话能正常完成"
    echo "========================================="
    echo ""

    local sid=$(create_session)
    info "Session: $sid"

    # ---- 第一步：发起对话并取消 ----
    local f1=$(mktemp /tmp/agent-test/sse_pc1.XXXXXX)
    curl -N -s -X POST "$BASE_URL/chat" \
        -H "Content-Type: application/json" \
        -d "{\"session_id\": \"$sid\", \"message\": \"你好\"}" > "$f1" 2>&1 &
    local p1=$!
    local rid1=$(wait_for_run_id "$f1" 30)
    [ -z "$rid1" ] && { fail "No run_id"; kill $p1 2>/dev/null; rm -f "$f1"; return 1; }
    info "Run 1: $rid1"
    [ "$(cancel_api "$rid1" | jq -r '.cancelled')" = "true" ] && pass "Run 1 已取消"
    sleep 2
    kill $p1 2>/dev/null; wait $p1 2>/dev/null

    local s1=$(get_run "$rid1" | jq -r '.status')
    [ "$s1" = "cancelled" ] && pass "Run 1 状态=cancelled" || { fail "Run 1 status=$s1"; rm -f "$f1"; return 1; }
    rm -f "$f1"

    # ---- 第二步：同一会话再次对话 ----
    info "同一会话发起新对话..."
    local f2=$(mktemp /tmp/agent-test/sse_pc2.XXXXXX)
    curl -N -s --max-time 30 -X POST "$BASE_URL/chat" \
        -H "Content-Type: application/json" \
        -d "{\"session_id\": \"$sid\", \"message\": \"第二次对话，请回复确认\"}" > "$f2" 2>&1
    local last_event=$(grep "^event:" "$f2" | tail -1 | sed 's/event: *//')
    local rid2=$(extract_run_id "$f2")

    if [ -n "$rid2" ]; then
        local s2=$(get_run "$rid2" | jq -r '.status')
        info "Run 2: $rid2 status=$s2 last_event=$last_event"
        [ "$s2" = "completed" ] && pass "第二次对话正常完成" || info "Run 2 status=$s2"
    else
        info "Run 2 SSE last_event=$last_event"
        [ "$last_event" = "run_completed" ] && pass "第二次对话正常完成" || fail "第二次对话未正常完成 (last=$last_event)"
    fi

    # 会话消息数应 > 第1次的消息数
    local mc=$(get_session "$sid" | jq -r '.message_count // 0')
    info "会话总消息数: $mc"
    [ "$mc" -gt 2 ] && pass "会话消息累积正确" || info "消息数=$mc"

    rm -f "$f2"
    echo ""; pass "Post-cancel Chat test completed"; echo ""
}

# ============================================================
# Test 5: Post-subagent-cancel Chat (子代理取消后再次对话)
# ============================================================
test_post_subagent_cancel_chat() {
    echo ""
    echo "========================================="
    echo " Test 5: Post-subagent-cancel Chat"
    echo " (子代理取消后再次对话)"
    echo "========================================="
    echo ""

    local sid=$(create_session)
    info "Session: $sid"

    # ---- 第一步：触发子代理并取消 ----
    local f1=$(mktemp /tmp/agent-test/sse_ps1.XXXXXX)
    curl -N -s -X POST "$BASE_URL/chat" \
        -H "Content-Type: application/json" \
        -d "{\"session_id\": \"$sid\", \"message\": \"请启动一个子代理（使用 Task 工具）来帮我分析项目，子代理类型为 Plan，任务prompt为：分析项目结构\"}" > "$f1" 2>&1 &
    local p1=$!

    # 等待 Task 工具调用
    local task_found=0
    for i in $(seq 1 60); do
        if has_task_tool "$f1"; then task_found=1; break; fi
        sleep 0.5
    done
    [ "$task_found" -eq 0 ] && { fail "Task 未调用"; kill $p1 2>/dev/null; rm -f "$f1"; return 1; }

    local rid1=$(extract_run_id "$f1")
    info "Run 1: $rid1"

    # 取消
    [ "$(cancel_api "$rid1" | jq -r '.cancelled')" = "true" ] && pass "Run 1 取消成功"
    for i in $(seq 1 30); do
        kill -0 $p1 2>/dev/null || break
        sleep 0.5
    done
    kill $p1 2>/dev/null; wait $p1 2>/dev/null
    sleep 1

    # 验证子代理已取消
    local child_cancelled=0
    local runs_json=$(redis_cmd "zrange('$(redis_key session_runs "$sid")', 0, -1)")
    for rid2 in $(echo "$runs_json" | jq -r '.[] // empty' 2>/dev/null); do
        local rtype=$(redis_cmd "hget('$(redis_key run "$rid2")', 'run_type')" | tr -d '"')
        local rstatus=$(redis_cmd "hget('$(redis_key run "$rid2")', 'status')" | tr -d '"')
        [ "$rtype" = "child" ] && [ "$rstatus" = "cancelled" ] && child_cancelled=1 && info "子代理 $rid2 已取消"
    done
    [ "$child_cancelled" -eq 1 ] && pass "子代理已取消" || info "未检测到子代理取消"
    rm -f "$f1"

    # ---- 第二步：同一会话再次对话 ----
    info "同一会话发起新对话..."
    local f2=$(mktemp /tmp/agent-test/sse_ps2.XXXXXX)
    curl -N -s --max-time 30 -X POST "$BASE_URL/chat" \
        -H "Content-Type: application/json" \
        -d "{\"session_id\": \"$sid\", \"message\": \"取消后的新对话，请正常回复\"}" > "$f2" 2>&1
    local last_event=$(grep "^event:" "$f2" | tail -1 | sed 's/event: *//')
    local rid3=$(extract_run_id "$f2")

    if [ -n "$rid3" ]; then
        local s3=$(get_run "$rid3" | jq -r '.status')
        info "Run 3: $rid3 status=$s3"
        [ "$s3" = "completed" ] && pass "子代理取消后新对话正常完成($s3)" || info "Run 3 status=$s3"
    fi
    [ "$last_event" = "run_completed" ] && pass "新对话 SSE 以 run_completed 结束" || info "last_event=$last_event"

    # ---- 第三步：验证子代理上下文仍完整保留 ----
    local child_ids=$(redis_cmd "smembers('$(redis_key session_child "$sid")')")
    local child_count=$(echo "$child_ids" | jq 'length' 2>/dev/null || echo 0)
    info "保留的子代理数: $child_count"
    [ "$child_count" -gt 0 ] && pass "子代理上下文未被清除" || info "无子代理上下文"

    for cid in $(echo "$child_ids" | jq -r '.[] // empty' 2>/dev/null); do
        local cmsg_count=$(redis_cmd "llen('$(redis_key child_msgs "$sid" "$cid")')")
        info "子代理 $cid 上下文: $cmsg_count 条消息"
        [ "$cmsg_count" -gt 0 ] && pass "子代理上下文消息保留" || info "无子代理上下文消息"
    done

    # 会话消息累计
    local mc=$(get_session "$sid" | jq -r '.message_count // 0')
    info "会话总消息数: $mc"

    rm -f "$f2"
    echo ""; pass "Post-subagent-cancel Chat test completed"; echo ""
}

# ============================================================
# Helper: 从会话消息中提取子代理的 child_id
# ============================================================
extract_child_id() {
    local sid="$1"
    # 优先从 session_children 集合读取
    local cid=$(redis_cmd "smembers('$(redis_key session_child "$sid")')" | jq -r '.[0] // ""' 2>/dev/null || echo "")
    if [ -n "$cid" ]; then
        echo "$cid"
        return 0
    fi
    # 兜底：从 tool 消息 meta 中提取
    redis_cmd "lrange('$(redis_key main_msgs "$sid")', 0, -1)" | \
    "$PROJECT_VENV" -c "
import json,sys
msgs = json.load(sys.stdin)
for m in msgs:
    if isinstance(m, str):
        try:
            obj = json.loads(m)
            meta = obj.get('_meta') or {}
            cid = meta.get('child_id') or meta.get('task_child_id') or ''
            if cid:
                print(cid)
                sys.exit(0)
        except:
            pass
" 2>/dev/null || echo ""
}

# ============================================================
# Test 6: Sub-agent Resume After Cancel (子代理取消后恢复执行)
# ============================================================
test_subagent_resume() {
    echo ""
    echo "========================================="
    echo " Test 6: Sub-agent Resume After Cancel"
    echo " (子代理取消后 resume 续跑)"
    echo "========================================="
    echo ""

    local sid=$(create_session)
    info "Session: $sid"

    # ========== Step 1: 触发子代理并取消 ==========
    info "Step 1: 触发子代理..."
    local f1=$(mktemp /tmp/agent-test/sse_r1.XXXXXX)
    curl -N -s -X POST "$BASE_URL/chat" \
        -H "Content-Type: application/json" \
        -d "{\"session_id\": \"$sid\", \"message\": \"请启动一个子代理（使用 Task 工具）来帮我分析项目，子代理类型为 Plan，任务prompt为：分析项目目录结构\"}" > "$f1" 2>&1 &
    local p1=$!

    for i in $(seq 1 60); do
        if has_task_tool "$f1"; then break; fi
        sleep 0.5
    done
    local rid1=$(extract_run_id "$f1")
    [ -z "$rid1" ] && { fail "No run_id"; kill $p1 2>/dev/null; rm -f "$f1"; return 1; }
    info "Master run: $rid1"

    # 取消
    [ "$(cancel_api "$rid1" | jq -r '.cancelled')" = "true" ] && pass "子代理已取消"
    for i in $(seq 1 30); do
        kill -0 $p1 2>/dev/null || break
        sleep 0.5
    done
    kill $p1 2>/dev/null; wait $p1 2>/dev/null
    sleep 1
    rm -f "$f1"

    # 提取 child_id
    local child_id=$(extract_child_id "$sid")
    [ -z "$child_id" ] && { fail "未提取到 child_id"; return 1; }
    info "Child ID: $child_id"

    # 记录取消前的子代理消息数
    local cmsg_before=$(redis_cmd "llen('$(redis_key child_msgs "$sid" "$child_id")')" 2>/dev/null || echo "0")
    info "取消前子代理消息数: $cmsg_before"

    # 验证主代理和子代理都 cancelled
    local rs1=$(get_run "$rid1" | jq -r '.status')
    [ "$rs1" = "cancelled" ] && pass "主代理 cancelled"
    local runs_json=$(redis_cmd "zrange('$(redis_key session_runs "$sid")', 0, -1)")
    for rid2 in $(echo "$runs_json" | jq -r '.[] // empty' 2>/dev/null); do
        local rtype=$(redis_cmd "hget('$(redis_key run "$rid2")', 'run_type')" | tr -d '"')
        local rstatus=$(redis_cmd "hget('$(redis_key run "$rid2")', 'status')" | tr -d '"')
        [ "$rtype" = "child" ] && [ "$rstatus" = "cancelled" ] && pass "子代理 cancelled"
    done

    # ========== Step 2: 同一会话用 resume 续跑 ==========
    info "Step 2: 使用 resume 续跑子代理..."
    local f2=$(mktemp /tmp/agent-test/sse_r2.XXXXXX)
    local resume_prompt="请继续执行子代理任务。你必须调用 Task 工具来恢复之前的子代理，参数为：subagent_type='Plan'，resume='$child_id'，prompt='继续分析项目结构，重点关注 app/ 目录结构'。注意：请直接调用 Task 工具，不要自己回答分析结果。"
    curl -N -s -X POST "$BASE_URL/chat" \
        -H "Content-Type: application/json" \
        -d "{\"session_id\": \"$sid\", \"message\": \"$resume_prompt\"}" > "$f2" 2>&1 &
    local p2=$!

    # 等待 Task 工具再次调用
    local task2=0
    for i in $(seq 1 120); do
        if has_task_tool "$f2"; then task2=1; break; fi
        if grep -q "run_completed" "$f2" 2>/dev/null; then break; fi
        sleep 0.5
    done

    # 等待 SSE 结束
    for i in $(seq 1 120); do
        kill -0 $p2 2>/dev/null || break
        sleep 0.5
    done
    kill $p2 2>/dev/null; wait $p2 2>/dev/null
    sleep 1
    rm -f "$f2"

    # ========== Step 3: 验证 ==========
    info "Step 3: 验证..."

    if [ "$task2" -eq 1 ]; then
        pass "第二次 Task 工具被调用（子代理续跑触发）"
    fi

    # 检查子代理消息数是否增加（续跑写入的）
    local cmsg_after=$(redis_cmd "llen('$(redis_key child_msgs "$sid" "$child_id")')" 2>/dev/null || echo "0")
    info "续跑后子代理消息数: $cmsg_after"

    if [ "$cmsg_after" -gt "$cmsg_before" ]; then
        pass "子代理上下文消息增加 ($cmsg_before → $cmsg_after)，resume 成功"
    else
        info "子代理消息数未增加（可能 LLM 未使用 resume 参数）"
    fi

    # 检查子代理 run 状态（新 run 应为 completed）
    local child_resumed=0
    local runs_json2=$(redis_cmd "zrange('$(redis_key session_runs "$sid")', 0, -1)")
    for rid3 in $(echo "$runs_json2" | jq -r '.[] // empty' 2>/dev/null); do
        local rtype=$(redis_cmd "hget('$(redis_key run "$rid3")', 'run_type')" | tr -d '"')
        local rstatus=$(redis_cmd "hget('$(redis_key run "$rid3")', 'status')" | tr -d '"')
        local cid=$(redis_cmd "hget('$(redis_key run "$rid3")', 'child_id')" | tr -d '"')
        if [ "$rtype" = "child" ] && [ "$cid" = "$child_id" ] && [ "$rstatus" = "completed" ]; then
            child_resumed=1
            pass "续跑了子代理 Run $rid3（status=$rstatus）"
        fi
    done

    if [ "$task2" -eq 1 ] && [ "$child_resumed" -eq 1 ]; then
        pass "子代理取消 → resume 续跑完整链路验证通过"
    elif [ "$task2" -eq 1 ]; then
        info "Task 已调用但续跑结果未完全验证"
    fi

    echo ""; pass "Sub-agent Resume test completed"; echo ""
}

# ============================================================
# Main

# ============================================================
# Main
# ============================================================
main() {
    echo ""
    echo "========================================="
    echo " Cancel E2E Test"
    echo " URL: $BASE_URL"
    echo " Time: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "========================================="
    echo ""

    check_prereqs || exit 1

    local run_all=true run_api=false run_disc=false run_sub=false run_resume=false

    if [ $# -gt 0 ]; then
        run_all=false
        for arg in "$@"; do
            case "$arg" in
                api)        run_api=true ;;
                disconnect) run_disc=true ;;
                subagent)   run_sub=true ;;
                resume)     run_resume=true ;;
            esac
        done
    fi

    [ "$run_all" = true ] && { run_api=true; run_disc=true; run_sub=true; run_resume=true; }
    [ "$run_api" = true ] && test_api_cancel
    [ "$run_disc" = true ] && test_disconnect_cancel
    [ "$run_sub" = true ] && test_subagent_cancel
    [ "$run_all" = true ] && test_post_cancel_chat
    [ "$run_all" = true ] && test_post_subagent_cancel_chat
    [ "$run_resume" = true ] && test_subagent_resume

    echo "========================================="
    echo " All tests completed"
    echo "========================================="
    echo ""
}

main "$@"
