# gov_crawler 部署文档

## 前置条件

服务器上已有：
- Docker + Docker Compose
- Redis（端口 16379，密码 `mypassword123`）- 如果没有，看第3步
- WaterCrawl 已运行在 `http://本机IP:7109`

---

## 1. 克隆代码

```bash
cd /data
git clone https://github.com/Ryan-01922/WaterCrawl.git
cd WaterCrawl/gov_crawler
```

---

## 2. 获取 WaterCrawl API Key

全程命令行，不需要浏览器。

### 2.1 创建管理员用户（如果还没创建）

```bash
cd /data/WaterCrawl/docker
docker compose exec app python manage.py createsuperuser
# 按提示输入：用户名、邮箱、密码（记好密码）
```

### 2.2 登录获取 JWT Token

```bash
# 用刚刚创建的管理员账号登录
TOKEN=$(curl -s -X POST http://本机IP:7109/api/v1/user/auth/login/ \
  -H "Content-Type: application/json" \
  -d '{"email":"你创建的邮箱","password":"你创建的密码"}' \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['access'])")
echo "JWT Token: $TOKEN"
```

### 2.3 用 Token 创建 API Key

```bash
# 调用 API Keys 接口创建 key，返回结果中的 key 字段就是 API Key
curl -s -X POST http://本机IP:7109/api/v1/user/api-keys/ \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "gov-crawler-key"}' | python3 -m json.tool --no-ensure-ascii
```

输出示例（`key` 以 `wc-` 开头就是 API Key）：

```json
{
  "uuid": "550e8400-e29b-41d4-a716-446655440000",
  "name": "gov-crawler-key",
  "key": "wc-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "created_at": "2026-07-25T12:00:00Z",
  "last_used_at": null
}
```

把 `key` 的值记下来，下一步填到 `WATERCRAWL_API_KEY` 里（仅在此次创建时返回一次）。

---

## 3. 确保 Redis 可用

如果服务器上还没有 Redis：

```bash
docker run -d --name redis-gov \
  --restart unless-stopped \
  -p 16379:6379 \
  redis:latest redis-server --requirepass mypassword123
```

---

## 4. 配置并启动 gov_crawler

```bash
cd /data/WaterCrawl/gov_crawler
cp .env.example .env
```

编辑 `.env` 文件：

```ini
# WaterCrawl API（把 IP 换成实际的）
WATERCRAWL_BASE_URL=http://本机IP:7109/api/v1/core
WATERCRAWL_API_KEY=上一步获取到的API_Key

# GPUStack 大模型（不用改）
GPUSTACK_API_BASE=https://gpustack.stock.hnchasing.com/v1
GPUSTACK_API_KEY=你的GPUStack_API_Key
GPUSTACK_MODEL=qwen3-32b

# Redis（连宿主机上的 Redis）
REDIS_HOST=host.docker.internal
REDIS_PORT=16379
REDIS_DB=0
REDIS_PASSWORD=mypassword123
```

启动：

```bash
docker compose up -d --build
```

---

## 5. 验证

```bash
# 检查是否能调通
curl http://本机IP:7107/api/sources

# 提交一个爬取任务测试
TASK_ID=$(curl -s -X POST http://本机IP:7107/api/crawl \
  -H "Content-Type: application/json" \
  -d '{"url":"https://www.cac.gov.cn/wxzw/wxfb/A093702index_1.htm"}' \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['task_id'])")
echo "任务ID: $TASK_ID"

# 等它跑完
while true; do
  S=$(curl -s "http://本机IP:7107/api/status?task_id=$TASK_ID" | python3 -c "import json,sys;print(json.load(sys.stdin)['status'])")
  echo "状态: $S"
  [ "$S" = "finished" ] && break
  sleep 8
done

# 看结果
curl -s "http://本机IP:7107/api/results?task_id=$TASK_ID" | python3 -m json.tool --no-ensure-ascii | head -50
```

---

## 6. 常用命令

```bash
# 查看日志
docker compose -f /data/WaterCrawl/gov_crawler/docker-compose.yml logs -f

# 重启
docker compose -f /data/WaterCrawl/gov_crawler/docker-compose.yml down
docker compose -f /data/WaterCrawl/gov_crawler/docker-compose.yml up -d --build

# 查看 crawl 日志（过滤清洗相关）
docker logs gov-crawler 2>&1 | grep -E "清洗|批次|清洗完成"
```
