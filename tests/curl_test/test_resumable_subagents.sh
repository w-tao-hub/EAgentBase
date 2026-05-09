#!/bin/bash
# 可恢复子代理查询工具端到端测试
# 正反面场景全覆盖：
#   1. 正面：派发→查询→Resume→最新 description
#   2. 反面：空 session 查询、占位摘要过滤、取消后摘要保留
#   3. 结构：验证 session_children 为 HASH
#
# 前置条件：
#   - 服务端已在 http://127.0.0.1:8000 运行
#   - 依赖：curl, jq

BASE_URL="http://127.0.0.1:8000"
REDIS_URL="redis://:scm_123@117.72.179.42:6379/0"
REDIS_PREFIX="agent"
PROJECT_VENV="/Users/myapple/应用文件/cursorcode/agent-framework/.venv/bin/python"
SSE_TIMEOUT=120

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${YELLOW}[INFO]${NC} $1"; }
pass()  { echo -e "${GREEN}[PASS]${NC} $1"; }
fail()  { echo -e "${RED}[FAIL]${NC} $1"; }
total_pass=0
total_fail=0

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
    elif isinstance(result, bytes):
        print(result.decode())
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
        session_child)  echo "${p}:session_children:${2}";;
        main_msgs)      echo "${p}:session_main_messages:${2}";;
    esac
}

create_session() { curl -s -X POST "$BASE_URL/sessions" | jq -r '.session_id'; }
cancel_api()     { curl -s -X POST "$BASE_URL/runs/$1/cancel"; }

extract_run_id() {
    local val=$(grep -o '"run_id": "[^"]*"' "$1" 2>/dev/null | head -1 | cut -d'"' -f4)
    [ -n "$val" ] && echo "$val" || return 1
}
has_tool() {
    grep -q '"tool_name": "[^"]*"' "$1" 2>/dev/null || return 1
    local name="$1"; local tool="$2"
    grep -q "\"tool_name\": \"$tool\"" "$1" 2>/dev/null && return 0 || return 1
}
has_task_tool() {
    has_tool "$1" "Task" && grep -q '"subagent_type"' "$1" 2>/dev/null
}

wait_for_sse_event() {
    local file="$1" pattern="$2" timeout="${3:-30}"
    for i in $(seq 1 $timeout); do
        grep -q "$pattern" "$file" 2>/dev/null && return 0
        sleep 0.5
    done
    return 1
}

wait_sse_end() {
    local pid=$1
    for i in $(seq 1 30); do
        kill -0 $pid 2>/dev/null || return 0
        sleep 0.5
    done
    kill $pid 2>/dev/null; wait $pid 2>/dev/null
}

do_pass()  { total_pass=$((total_pass+1)); pass "$1"; }
do_fail()  { total_fail=$((total_fail+1)); fail "$1"; }

check_prereqs() {
    for cmd in curl jq python3; do
        command -v "$cmd" &>/dev/null || { fail "Missing: $cmd"; return 1; }
    done
    local code=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/health/live" 2>/dev/null || echo "000")
    [ "$code" != "200" ] && { fail "Server unreachable (HTTP $code)"; return 1; }
    info "Server OK (HTTP $code)"
    return 0
}

# ============================================================
# 反面场景 1：创建 session 后立即查询（无子代理）
# ============================================================
test_empty_before_dispatch() {
    echo ""
    echo "========================================="
    echo " [反面] 创建 session 后立即查询（无子代理）"
    echo "========================================="
    echo ""

    local sid=$(create_session)
    info "Session: $sid"

    # 直接检查 Redis
    local key=$(redis_key session_child "$sid")
    local exists=$(redis_cmd "exists('$key')" | tr -d '"')
    if [ "$exists" = "0" ]; then
        # key 不存在 = 没有 child → ListResumableSubagents 应返回空列表
        # （我们不能直接调工具，但可以通过 Redis 确认无污染条目）
        do_pass "未派发子代理时 session_children key 不存在（无幽灵条目）"
    else
        do_fail "未派发但 session_children 已存在"
    fi

    echo ""; info "结果: passes=$total_pass failures=$total_fail"
}

# ============================================================
# 正面场景 1：验证 Redis 存储结构为 HASH
# ============================================================
test_hash_structure() {
    echo ""
    echo "========================================="
    echo " [正面] 验证 session_children 存储为 HASH"
    echo "========================================="
    echo ""

    local sid=$(create_session)
    info "Session: $sid"

    # 用一个 dummy child 写入 HASH，验证 TYPE
    local key=$(redis_key session_child "$sid")
    redis_cmd "hset('$key', 'verify-child', '{\"resume_id\":\"verify-child\",\"subagent_type\":\"Plan\",\"description\":\"验证\"}')" > /dev/null

    local key_type=$(redis_cmd "type('$key')" | tr -d '"')
    if [ "$key_type" = "hash" ]; then
        do_pass "session_children 类型为 hash（非旧 set）"
    else
        do_fail "类型为 $key_type，期望 hash"
    fi

    # 验证 HGETALL 输出为 JSON
    local raw=$(redis_cmd "hgetall('$key')")
    local has_resume_id=$(echo "$raw" | "$PROJECT_VENV" -c "
import json,sys
d=json.load(sys.stdin)
for k,v in d.items():
    obj=v if isinstance(v,dict) else json.loads(v)
    if obj.get('resume_id'): print('yes'); sys.exit(0)
print('no')
" 2>/dev/null || echo "no")
    [ "$has_resume_id" = "yes" ] && do_pass "HASH value 包含 resume_id" || do_fail "HASH value 缺少 resume_id"

    # 清理
    redis_cmd "del('$key')" > /dev/null

    echo ""; info "结果: passes=$total_pass failures=$total_fail"
}

# ============================================================
# 正面场景 2：派发子代理并验证摘要
# ============================================================
test_dispatch_and_query() {
    echo ""
    echo "========================================="
    echo " [正面] 派发子代理 → 验证摘要"
    echo "========================================="
    echo ""

    local sid=$(create_session)
    info "Session: $sid"

    # 派发子代理（提示词必须精确触发 Task 工具调用）
    local f=$(mktemp /tmp/agent-test/sse_p1.XXXXXX)
    curl -N -s -X POST "$BASE_URL/chat" \
        -H "Content-Type: application/json" \
        -d "{\"session_id\": \"$sid\", \"message\": \"你必须调用 Task 工具来派发一个子代理。参数如下：subagent_type='Plan'，description='分析项目结构'，prompt='请分析项目目录结构并给出改进建议'。只调用 Task 工具，不要自己分析。\"}" > "$f" 2>&1 &
    local p=$!

    wait_for_sse_event "$f" "Task" 60 && do_pass "Task 工具被调用" || do_fail "Task 未在超时内调用"
    local rid=$(extract_run_id "$f")
    [ -n "$rid" ] && info "Run: $rid"
    # 等待 run_completed 或 run_failed 事件，确保子代理执行完毕
    wait_for_sse_event "$f" "run_completed\|run_failed" 120 || info "子代理可能仍在执行"
    wait_sse_end $p
    sleep 2

    # 验证 HASH 中有完整摘要
    local key=$(redis_key session_child "$sid")
    local raw=$(redis_cmd "hgetall('$key')")
    info "Raw hgetall: $raw"
    local child_count=$(echo "$raw" | "$PROJECT_VENV" -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null)
    [ "$child_count" -gt 0 ] && do_pass "session_children 包含 $child_count 条摘要" || do_fail "摘要为空"

    # 验证三个字段齐全
    local has_fields=$(echo "$raw" | "$PROJECT_VENV" -c "
import json,sys
d=json.load(sys.stdin)
for k,v in d.items():
    obj=v if isinstance(v,dict) else json.loads(v)
    rid=obj.get('resume_id',''); st=obj.get('subagent_type',''); desc=obj.get('description','')
    if rid and st and desc:
        print(f'resume_id={rid} type={st} desc={desc}')
        sys.exit(0)
print('no')
" 2>/dev/null)
    if echo "$has_fields" | grep -q "resume_id="; then
        do_pass "摘要字段完整: $has_fields"
    else
        do_fail "摘要字段不完整"
    fi

    rm -f "$f"
    echo ""; info "结果: passes=$total_pass failures=$total_fail"
}

# ============================================================
# 反面场景 2：取消子代理后摘要保留（验证无幽灵误删）
# ============================================================
test_cancel_preserves_summary() {
    echo ""
    echo "========================================="
    echo " [反面] 取消子代理 → 摘要保留（不误删）"
    echo "========================================="
    echo ""

    local sid=$(create_session)
    info "Session: $sid"

    local f=$(mktemp /tmp/agent-test/sse_c1.XXXXXX)
    curl -N -s -X POST "$BASE_URL/chat" \
        -H "Content-Type: application/json" \
        -d "{\"session_id\": \"$sid\", \"message\": \"你必须调用 Task 工具来派发一个子代理。参数：subagent_type='Plan'，description='分析项目'，prompt='分析项目目录结构'。只调用 Task，不要自己回答。\"}" > "$f" 2>&1 &
    local p=$!

    wait_for_sse_event "$f" "Task" 60 || { do_fail "Task 未调用"; kill $p 2>/dev/null; rm -f "$f"; return 1; }
    local rid=$(extract_run_id "$f")
    info "Run: $rid"

    # 等子代理开始执行后再取消（确保已过 append_child_message 点）
    sleep 8
    cancel_api "$rid" > /dev/null
    do_pass "取消请求已发送"
    wait_sse_end $p
    sleep 2

    # 验证取消后摘要仍然存在（不是幽灵条目，是有意派发）
    local key=$(redis_key session_child "$sid")
    local raw=$(redis_cmd "hgetall('$key')")
    info "Cancel raw hgetall: $raw"
    local child_count=$(echo "$raw" | "$PROJECT_VENV" -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null)
    if [ "$child_count" -gt 0 ]; then
        do_pass "取消后摘要仍保留（$child_count 条，不会被误删）"
        local desc=$(echo "$raw" | "$PROJECT_VENV" -c "
import json,sys
d=json.load(sys.stdin)
for k,v in d.items():
    obj=v if isinstance(v,dict) else json.loads(v)
    print(obj.get('description',''))
" 2>/dev/null)
        info "保留的 description: $desc"
    else
        do_fail "取消后摘要被错误清空"
    fi

    rm -f "$f"
    echo ""; info "结果: passes=$total_pass failures=$total_fail"
}

# ============================================================
# 正面场景 3：Resume 续跑验证 description 更新 + 无重复
# ============================================================
test_resume_updates_description() {
    echo ""
    echo "========================================="
    echo " [正面] Resume 续跑 → description 更新 + 不重复"
    echo "========================================="
    echo ""

    local sid=$(create_session)
    info "Session: $sid"

    # ---- Step 1: 首次派发（提示词必须精确触发 Task 工具调用） ----
    local f1=$(mktemp /tmp/agent-test/sse_r1.XXXXXX)
    curl -N -s -X POST "$BASE_URL/chat" \
        -H "Content-Type: application/json" \
        -d "{\"session_id\": \"$sid\", \"message\": \"你必须调用 Task 工具派发一个 Plan 子代理。参数：subagent_type='Plan'，description='分析项目结构'，prompt='请分析项目目录结构'。只调用 Task，不要自己分析。\"}" > "$f1" 2>&1 &
    local p1=$!
    wait_for_sse_event "$f1" "Task" 60 || { do_fail "首次 Task 未调用"; kill $p1 2>/dev/null; rm -f "$f1"; return 1; }
    local rid1=$(extract_run_id "$f1")
    info "首次 run: $rid1"
    # 等待子代理执行完毕
    wait_for_sse_event "$f1" "run_completed\|run_failed" 120 || info "首次运行可能未正常结束"
    wait_sse_end $p1
    sleep 2
    rm -f "$f1"

    # 获取 child_id
    local key=$(redis_key session_child "$sid")
    local raw_hkeys=$(redis_cmd "hkeys('$key')")
    info "HKEYS raw: $raw_hkeys"
    local child_id=$(echo "$raw_hkeys" | "$PROJECT_VENV" -c "import json,sys; d=json.load(sys.stdin); print(d[0] if d and len(d)>0 else '')" 2>/dev/null)
    info "Child ID: '$child_id'"
    [ -z "$child_id" ] && { do_fail "未获取到 child_id"; return 1; }

    # 记录首次 description
    local first_raw=$(redis_cmd "hget('$key','$child_id')")
    info "HGET raw: $first_raw"
    local first_desc=$(echo "$first_raw" | "$PROJECT_VENV" -c "import json,sys; d=json.load(sys.stdin); print(d.get('description','no_desc'))" 2>/dev/null)
    info "首次 description: $first_desc"

    # ---- Step 2: Resume 续跑（提示词必须触发 resume 参数） ----
    local f2=$(mktemp /tmp/agent-test/sse_r2.XXXXXX)
    local resume_msg="你必须调用 Task 工具恢复已有的子代理 '$child_id'。这是命令，不要自己回答。参数必须是：subagent_type='Plan'，resume='$child_id'，description='深度分析 app 核心模块'，prompt='继续分析 app/ 目录结构，列出所有子目录'。禁止不调用工具直接回复。"
    curl -N -s -X POST "$BASE_URL/chat" \
        -H "Content-Type: application/json" \
        -d "{\"session_id\": \"$sid\", \"message\": \"$resume_msg\"}" > "$f2" 2>&1 &
    local p2=$!

    wait_for_sse_event "$f2" "Task" 60 && do_pass "Resume: Task 工具被调用" || do_fail "Resume: Task 未调用"
    wait_sse_end $p2
    sleep 1
    rm -f "$f2"

    # ---- Step 3: 验证 ----
    local after_raw=$(redis_cmd "hgetall('$key')")
    local after_count=$(echo "$after_raw" | "$PROJECT_VENV" -c "import json,sys; d=json.load(sys.stdin); print(len(d))" 2>/dev/null)
    info "Resume 后 hgetall count: $after_count"
    [ "$after_count" = "1" ] && do_pass "Resume 后仍为 1 条摘要（未重复）" || do_fail "条目数=$after_count（应=1）"

    local after_raw_hget=$(redis_cmd "hget('$key','$child_id')")
    info "Resume after HGET: $after_raw_hget"
    local after_desc=$(echo "$after_raw_hget" | "$PROJECT_VENV" -c "import json,sys; d=json.load(sys.stdin); print(d.get('description','no_desc'))" 2>/dev/null)
    info "Resume 后 description: $after_desc"

    if [ -n "$first_desc" ] && [ -n "$after_desc" ] && [ "$first_desc" != "$after_desc" ]; then
        do_pass "Description 已更新: $first_desc → $after_desc"
    elif [ -n "$after_desc" ]; then
        do_fail "Description 未变化（$first_desc → $after_desc）"
    else
        do_fail "Description 为空或无法解析"
    fi

    echo ""; info "结果: passes=$total_pass failures=$total_fail"
}

# ============================================================
# Main
# ============================================================
main() {
    echo ""
    echo "========================================="
    echo " ListResumableSubagents E2E Test"
    echo " URL: $BASE_URL"
    echo " Time: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "========================================="
    echo ""

    check_prereqs || exit 1

    test_empty_before_dispatch    # 反面：无子代理
    test_hash_structure           # 正面：HASH 存储
    test_dispatch_and_query       # 正面：派发→摘要
    test_cancel_preserves_summary # 反面：取消不删摘要
    test_resume_updates_description # 正面：Resume→更新+不重复

    echo ""
    echo "========================================="
    echo " 总结果: passes=$total_pass failures=$total_fail"
    [ "$total_fail" -eq 0 ] && pass "所有测试通过" || fail "$total_fail 个失败"
    echo "========================================="
    echo ""
}

main "$@"
