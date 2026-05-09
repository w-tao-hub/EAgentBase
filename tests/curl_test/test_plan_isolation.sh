#!/bin/bash
# Plan 隔离 curl 验证
# 1. 主代理创建 plan → 2. Worker 创建 plan → 3. 验证互相不可见
BASE="http://127.0.0.1:8765"

chat() {
  # 发送 chat 消息，提取 run_completed 的 output
  curl -s -N -X POST "$BASE/chat" \
    -H "Content-Type: application/json" \
    -d "$1" 2>/dev/null | python3 -c "
import sys, json
for line in sys.stdin:
    if line.startswith('data: '):
        d = json.loads(line[6:])
        if d.get('type') == 'run_completed':
            print(d.get('output', ''))
            break
"
}

echo "=== 1. 创建会话 ==="
SESS=$(curl -s -X POST "$BASE/sessions" -H "Content-Type: application/json" -d '{"agent_id":"master"}')
SID=$(echo "$SESS" | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
echo "会话ID: $SID"

echo ""
echo "=== 2. 主代理创建 plan ==="
MASTER_OUT=$(chat "{\"session_id\":\"$SID\",\"message\":\"使用 plan_create 创建一个标题为'主代理任务'、描述为'主代理创建'的计划任务，然后使用 plan_list 列出结果。\"}")
echo "主代理: $MASTER_OUT"

echo ""
echo "=== 3. Worker 创建 plan ==="
WORKER_OUT=$(chat "{\"session_id\":\"$SID\",\"message\":\"使用 Task 工具调起 Worker 子代理，指定 tools 为 plan_create、plan_list，让 Worker 创建一个标题为'Worker任务'、描述为'Worker创建'的计划任务，然后列出计划列表。\"}")
echo "Worker: $WORKER_OUT"

echo ""
echo "=== 4. 主代理再次列出 plan ==="
MASTER_LIST=$(chat "{\"session_id\":\"$SID\",\"message\":\"使用 plan_list 列出当前所有计划任务。\"}")
echo "主代理列表: $MASTER_LIST"

echo ""
echo "=== 验证 ==="
HAS_MASTER=$(echo "$MASTER_LIST" | grep -c "主代理任务")
HAS_WORKER=$(echo "$MASTER_LIST" | grep -c "Worker任务")
echo "主代理创建 → $(echo "$MASTER_OUT" | grep -c '主代理任务' | awk '{if($1>0)print "✓"; else print "✗"}')"
echo "Worker创建  → $(echo "$WORKER_OUT" | grep -c 'Worker任务' | awk '{if($1>0)print "✓"; else print "✗"}')"
echo "主代理看到自己任务 → $(if [ $HAS_MASTER -gt 0 ]; then echo "✓"; else echo "✗"; fi)"
echo "主代理看到Worker任务 → $(if [ $HAS_WORKER -gt 0 ]; then echo "✗ 隔离失败!"; else echo "✓ 已隔离"; fi)"
