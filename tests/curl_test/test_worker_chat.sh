#!/bin/bash
# Worker 通用子代理 curl 测试
BASE="http://127.0.0.1:8765"

echo "=== 1. 创建会话 ==="
SESS=$(curl -s -X POST "$BASE/sessions" -H "Content-Type: application/json" -d '{"agent_id":"master"}')
SID=$(echo "$SESS" | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
echo "会话ID: $SID"

echo ""
echo "=== 2. 触发 Worker 子代理（创建计划任务）==="
# SSE 流式响应，只取前 30 行避免刷屏
curl -s -N -X POST "$BASE/chat" \
  -H "Content-Type: application/json" \
  -d "{\"session_id\":\"$SID\",\"message\":\"请使用 Task 工具调起 Worker 子代理，指定 tools 为 plan_create、plan_list，让 Worker 创建一个标题为'测试任务'、描述为'测试描述'的计划任务，然后列出所有计划。\"}" 2>&1 | head -30

echo ""
echo ""
echo "=== 3. 健康检查 ==="
curl -s "$BASE/health"
echo ""
echo "--- 完成 ---"
