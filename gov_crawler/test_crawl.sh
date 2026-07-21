#!/bin/bash
# Gov Crawler 测试脚本：提交爬取 + 轮询 + 输出完整 JSON
# 用法: bash test_crawl.sh [url] [host]
# 示例: bash test_crawl.sh
#        bash test_crawl.sh "https://www.ccgp.gov.cn"
#        bash test_crawl.sh "https://www.ccgp.gov.cn" "http://10.60.151.130:7107"

URL="${1:-https://www.ccgp.gov.cn}"
HOST="${2:-http://localhost:7107}"
OUTPUT_FILE="test_result_$(date +%Y%m%d_%H%M%S).json"

echo "========================================="
echo " Gov Crawler 测试"
echo " 目标: $URL"
echo " 服务: $HOST"
echo "========================================="

# 1. 提交任务
echo ""
echo "[1/3] 提交爬取任务..."
RESP=$(curl -s -X POST "$HOST/api/crawl" \
  -H "Content-Type: application/json" \
  -d "{\"url\":\"$URL\"}")

TASK_ID=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin)['task_id'])" 2>/dev/null)
if [ -z "$TASK_ID" ]; then
  echo "ERROR: 无法获取 task_id"
  echo "$RESP"
  exit 1
fi
echo "Task ID: $TASK_ID"

# 2. 轮询状态
echo ""
echo "[2/3] 等待任务完成..."
MAX_WAIT=300
ELAPSED=0
while [ $ELAPSED -lt $MAX_WAIT ]; do
  STATUS_JSON=$(curl -s "$HOST/api/status?task_id=$TASK_ID")
  STATUS=$(echo "$STATUS_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin).get('status','?'))" 2>/dev/null)
  PROGRESS=$(echo "$STATUS_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin).get('progress',''))" 2>/dev/null)
  echo "  [${ELAPSED}s] $STATUS - $PROGRESS"
  
  case "$STATUS" in
    finished|failed)
      break
      ;;
  esac
  sleep 8
  ELAPSED=$((ELAPSED + 8))
done

if [ $ELAPSED -ge $MAX_WAIT ]; then
  echo "ERROR: 超时 ${MAX_WAIT}s"
  exit 1
fi

# 3. 获取结果
echo ""
echo "[3/3] 获取结果..."
curl -s "$HOST/api/results?task_id=$TASK_ID" | python3 -m json.tool --no-ensure-ascii > "$OUTPUT_FILE"

echo ""
echo "========================================="
echo " 完成！结果已保存到: $OUTPUT_FILE"
echo "========================================="

# 4. 摘要
python3 -c "
import json
with open('$OUTPUT_FILE', 'r') as f:
    d = json.load(f)

total = d.get('total', 0)
results = d.get('results', [])
summary = d.get('summary', '')

print(f'\n文章总数: {total}')
print(f'成功爬取: {len(results)} 篇')
print(f'全局摘要: {len(summary)} 字')
print()

for i, r in enumerate(results):
    c = r.get('cleaned') or {}
    title = c.get('title', r.get('title', '?'))
    body = c.get('body', '')
    source = c.get('source', '')
    date = c.get('publish_date', '')
    print(f'[{i+1:2d}] {title[:60]}')
    if date: print(f'     日期: {date}', end='')
    if source: print(f'  来源: {source}', end='')
    print(f'\n     摘要({len(body)}字): {body[:100]}{\"...\" if len(body)>100 else \"\"}')
    print()
"
