# Gov Crawler — 通用政府网站文章爬取服务

基于 WaterCrawl API + Qwen3-32B AI + Redis 任务队列，实现**任意政府网站列表页**的两层自动化爬取。

---

## 目录

- [架构概览](#架构概览)
- [完整 Pipeline](#完整-pipeline)
- [API 文档](#api-文档)
- [环境变量](#环境变量)
- [部署](#部署)
- [调用示例](#调用示例)
- [项目结构](#项目结构)

---

## 架构概览

```
┌─────────────┐     ┌──────────────────┐     ┌───────────────┐
│   客户端     │────▶│   FastAPI API    │────▶│  Redis Queue  │
└─────────────┘     └──────────────────┘     └───────┬───────┘
                                                     │
                                     ┌───────────────┼───────────────┐
                                     │               │               │
                                     ▼               ▼               ▼
                               Worker[0]       Worker[1]       Worker[2]
                                     │               │               │
                                     └───────────────┼───────────────┘
                                                     │
                                                     ▼
                                           WaterCrawl API
                                           (7109 端口爬虫引擎)
                                                     │
                                                     ▼
                                             Qwen3-32B AI
                                          (链接识别 + 内容清洗 + 摘要)
```

**核心能力：**

| 能力 | 说明 |
|------|------|
| **通用性** | 不依赖固定网站结构，AI 自动识别文章链接 |
| **多级降级** | AI → URL 白名单 → `li a` 选择器 → 全页面启发式 |
| **任务队列** | Redis + 3 Worker，支持并发提交，互不阻塞 |
| **iframe 适配** | 自动检测并追加爬取 iframe 内容 |
| **内容清洗** | AI 提取 title/date/source/body，去除导航和页脚 |
| **智能摘要** | AI 基于文章标题生成 200 字摘要 |
| **30 篇上限** | 链接列表无限制，实际爬取正文限制前 30 篇 |

---

## 完整 Pipeline

### 总体流程

```
POST /api/crawl {"url": "..."}
        │
        ▼
┌─ Step 0: 任务入队 ──────────────────────────────────┐
│  • 生成 task_id（8 位 UUID）                         │
│  • 写入 Redis Hash（status=pending）                 │
│  • RPUSH 到队列 gov_crawler:queue                    │
│  • 异步返回 {"task_id": "a1b2c3d4", "status":"queued"}│
└─────────────────────────────────────────────────────┘
        │
        ▼
┌─ Step 1: Worker 抢任务 ─────────────────────────────┐
│  Worker[0] brpop → 拿到 task_id                     │
│  更新 status=running                                 │
└─────────────────────────────────────────────────────┘
        │
        ▼
┌─ Step 2: Layer 1 — 爬取列表页 ─────────────────────┐
│  2a. WaterCrawl POST /crawl-requests/                │
│      Body: {"url": target_url, "options": {           │
│        "include_html": true,                         │
│        "wait_time": 2000,    ← 等 2 秒给 JS 渲染   │
│        "only_main_content": false                    │
│      }}                                             │
│                                                      │
│  2b. 轮询 GET /crawl-requests/{uuid}/                │
│      直到 status=finished（最长 300s）               │
│                                                      │
│  2c. GET /crawl-requests/{uuid}/results/             │
│      → result 字段是 MinIO 预签名 URL                │
│      → 替换 localhost 后下载 JSON → 拿到 HTML        │
└─────────────────────────────────────────────────────┘
        │
        ▼
┌─ Step 3: iframe 检测 ───────────────────────────────┐
│  3a. BeautifulSoup 解析 HTML                         │
│  3b. find_all("iframe", src=True)                    │
│      ├── 找到 iframe → urljoin 拼出完整 URL           │
│      └── 没找到 → 跳过，继续下一步                    │
│                                                      │
│  3c. 对每个 iframe 再次调用 WaterCrawl 爬取          │
│      → 将 iframe 的 HTML 合并到主 HTML                │
│      → html_len 从 2K 增长到 30K+                    │
└─────────────────────────────────────────────────────┘
        │
        ▼
┌─ Step 4: AI 识别文章链接（三层降级） ────────────────┐
│                                                      │
│  阶段1: 提取候选链接                                  │
│  ├── BeautifulSoup 提取所有 <a href>                  │
│  ├── 过滤: javascript: / # / mailto: / pdf / docx    │
│  ├── 过滤: 链接文本为空                               │
│  └── 保留: 所有 http URL → 200~500 个候选             │
│                                                      │
│  阶段2: Qwen3-32B AI 识别                            │
│  ├── 全量候选链接发给 AI                              │
│  ├── Prompt: "从以下链接列表中筛选出正文文章链接"       │
│  ├── 返回: [{title, url}, ...]                       │
│  └── 结果 >= 3 → 直接使用 ✓                          │
│                                                      │
│  阶段3: 降级策略（AI 失败时）                         │
│  3a. URL 白名单正则匹配                               │
│      /art/ | /t{日期}_{编号}.htm | /c_{编号}.htm      │
│      | /content/ | /info/ | /xxgk/.*/\d+ 等 10 种     │
│  3b. <li> <a href> 通用选择器                        │
│  3c. 全页面链接 + 启发式过滤                          │
│      （排除 /col/col\d, 短文本, 导航关键词）          │
│                                                      │
│  合并策略:                                           │
│  • AI 返回 >= 3 篇 → 仅用 AI 结果                    │
│  • AI 返回 1-2 篇 → 合并 AI+降级（取并集去重）       │
│  • AI 返回 0 篇 → 全部用降级结果                     │
└─────────────────────────────────────────────────────┘
        │
        ▼  articles_info = [{title, url}, ...]
        │
┌─ Step 5: Layer 2 — 批量爬取文章 ───────────────────┐
│  5a. 截断: article_urls[:30]                        │
│      → 全量链接存入 Redis（/api/results 中可查）     │
│      → 仅前 30 篇进入正文爬取                        │
│                                                      │
│  5b. WaterCrawl POST /crawl-requests/batch/          │
│      Body: {"urls": [...], "options": {               │
│        "include_html": true,                         │
│        "only_main_content": true,  ← 只抓正文       │
│        "wait_time": 1000                             │
│      }}                                             │
│                                                      │
│  5c. 轮询等待 batch 任务完成（最长 600s）             │
│  5d. 下载每篇文章的 MinIO 结果文件                    │
│      → batch_results = ["文章1 HTML", "文章2 HTML", ...]│
└─────────────────────────────────────────────────────┘
        │
        ▼
┌─ Step 6: AI 内容清洗 ───────────────────────────────┐
│  6a. 截取每篇文章前 5000 字符                         │
│  6b. 用 ===== 分隔拼接所有文章                        │
│  6c. 一次性发送给 Qwen3-32B（批量调用，节省 API 次数）│
│                                                      │
│  Prompt: "为每篇文章生成 200 字以内的摘要，概括核心内容"   │
│                                                      │
│  返回分隔符格式 (TTL/DTM/SRC/ABS)                     │
│  解析：split("===") → 前缀匹配                        │
│                                                      │
│  cleaned = [{title, publish_date, source, body}, ...] │
│  (body 字段存储摘要内容)                              │
└─────────────────────────────────────────────────────┘
        │
        ▼
┌─ Step 7: AI 摘要 ───────────────────────────────────┐
│  7a. 收集所有清洗后的标题                             │
│  7b. 发送给 Qwen3-32B                                │
│      Prompt: "根据以下 {N} 篇政府政策文章标题，       │
│              写一个 200 字以内摘要"                   │
│  7c. 返回一段概括性文字                               │
└─────────────────────────────────────────────────────┘
        │
        ▼
┌─ Step 8: 存储结果 ──────────────────────────────────┐
│  Redis Hash: gov_crawler:task:{task_id}              │
│  ├── links: JSON [{title, url}, ...]    全部链接     │
│  ├── results: JSON [{title, url, content, cleaned}]  │
│  ├── summary: 摘要文本                               │
│  ├── status: "finished"                              │
│  └── progress: "全部完成: 15 篇文章 + AI 摘要"        │
└─────────────────────────────────────────────────────┘
```

### Pipeline 时序图

```
客户端          FastAPI         Redis          Worker         WaterCrawl      Qwen3-32B
  │               │               │               │               │               │
  │ POST /crawl   │               │               │               │               │
  │──────────────▶│               │               │               │               │
  │               │ RPUSH queue   │               │               │               │
  │               │──────────────▶│               │               │               │
  │ task_id       │               │               │               │               │
  │◀──────────────│               │               │               │               │
  │               │               │               │               │               │
  │ GET /status   │               │  brpop        │               │               │
  │──────────────▶│               │◀──────────────│               │               │
  │ status=running│               │ task_id       │               │               │
  │◀──────────────│               │──────────────▶│               │               │
  │               │               │               │               │               │
  │  ...轮询...   │               │               │ Layer 1 爬    │               │
  │               │               │               │──────────────▶│               │
  │               │               │               │ HTML          │               │
  │               │               │               │◀──────────────│               │
  │               │               │               │               │               │
  │               │               │               │ AI 识别链接   │               │
  │               │               │               │──────────────────────────────▶│
  │               │               │               │ [{title,url}]                 │
  │               │               │               │◀──────────────────────────────│
  │               │               │               │               │               │
  │               │               │               │ Layer 2 batch │               │
  │               │               │               │──────────────▶│               │
  │               │               │               │ 文章内容      │               │
  │               │               │               │◀──────────────│               │
  │               │               │               │               │               │
  │               │               │               │ AI 清洗 + 摘要│               │
  │               │               │               │──────────────────────────────▶│
  │               │               │               │ cleaned + summary             │
  │               │               │               │◀──────────────────────────────│
  │               │               │               │               │               │
  │               │               │ 存储结果 HSET  │               │               │
  │               │               │◀──────────────│               │               │
  │               │               │               │               │               │
  │ GET /results  │               │               │               │               │
  │──────────────▶│ HGETALL       │               │               │               │
  │ results       │──────────────▶│               │               │               │
  │◀──────────────│◀──────────────│               │               │               │
```

---

## API 文档

### 预设站点

```
GET /api/sources
```

```json
{
  "sources": [
    {"name": "财政部税政司 - 政策发布", "url": "https://szs.mof.gov.cn/zhengcefabu/"},
    {"name": "中央网信办 - 网信发布", "url": "https://www.cac.gov.cn/wxzw/wxfb/A093702index_1.htm"}
  ]
}
```

### 启动爬取

```
POST /api/crawl
Content-Type: application/json
{"url": "https://szs.mof.gov.cn/zhengcefabu/"}
```

```json
{"status": "queued", "task_id": "a1b2c3d4", "url": "..."}
```

> 入队后立即返回，不阻塞。同时可提交多个 URL，由 Worker 并发执行。

### 查询状态

```
GET /api/status?task_id=a1b2c3d4
```

```json
{"task_id": "a1b2c3d4", "status": "running", "progress": "Layer 2: 正在批量爬取 10 篇文章...", "url": "...", "created_at": "..."}
```

| status | 含义 |
|--------|------|
| `pending` | 在队列中等待 |
| `running` | Worker 正在执行 |
| `finished` | 爬取完成 |
| `failed` | 爬取失败 |

### 获取结果

```
GET /api/results?task_id=a1b2c3d4
```

```json
{
  "task_id": "a1b2c3d4",
  "status": "finished",
  "progress": "全部完成: 10 篇文章 + AI 摘要",
  "links": [
    {"title": "关于xxx的通知", "url": "https://..."},
    {"title": "关于yyy的公告", "url": "https://..."}
  ],
  "total": 15,
  "results": [
    {
      "title": "关于xxx的通知",
      "url": "https://szs.mof.gov.cn/...",
      "content": "<html>原始页面全文</html>",
      "cleaned": {
        "title": "关于xxx的通知",
        "publish_date": "2026-07-10",
        "source": "财政部",
        "body": "本文摘要内容..."
      }
    }
  ],
  "summary": "本批文章主要涵盖三大方向：一是..."
}
```

> `links` 返回**全部**识别到的文章链接（无 30 篇上限）；`results` 包含实际爬取了正文的**前 30 篇**。

---

## 环境变量

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `WATERCRAWL_API_KEY` | **是** | — | WaterCrawl API 密钥 |
| `WATERCRAWL_BASE_URL` | 否 | `http://10.60.151.130:7109/api/v1/core` | WaterCrawl API 地址 |
| `GPUSTACK_API_KEY` | **是** | — | GPUStack API 密钥 |
| `GPUSTACK_API_BASE` | 否 | `https://gpustack.stock.hnchasing.com/v1` | GPUStack API 地址 |
| `GPUSTACK_MODEL` | 否 | `qwen3-32b` | 使用的模型名称 |
| `REDIS_HOST` | 否 | `host.docker.internal` | Redis 宿主机地址 |
| `REDIS_PORT` | 否 | `16379` | Redis 端口 |
| `REDIS_DB` | 否 | `0` | Redis 数据库编号 |
| `REDIS_PASSWORD` | 否 | — | Redis 密码 |
| `REDIS_URL` | 否 | (由上述变量自动拼接) | 或直接指定完整 URL |
| `WORKER_COUNT` | 否 | `3` | 并发 Worker 数量 |

---

## 部署

### 前提

- Docker & Docker Compose
- WaterCrawl 服务已在 `10.60.151.130:7109` 运行
- GPUStack API 已就绪

### 步骤

```bash
cd gov_crawler

# 1. 创建环境变量文件
cp .env.example .env
nano .env  # 填入 WATERCRAWL_API_KEY 和 GPUSTACK_API_KEY

# 2. 启动（连接宿主机 Redis）
sudo docker compose up -d --build

# 3. 验证
sudo docker logs gov-crawler --tail 10
# 应看到: "已启动 3 个工作进程"
```

### 验证 Worker

```bash
sudo docker logs gov-crawler | grep "Worker"
# Worker[0] 已启动
# Worker[1] 已启动
# Worker[2] 已启动
```

---

## 调用示例

### Bash

```bash
# 1. 启动爬取
TASK=$(curl -s -X POST http://10.60.151.130:7107/api/crawl \
  -H "Content-Type: application/json" \
  -d '{"url": "https://szs.mof.gov.cn/zhengcefabu/"}')
TASK_ID=$(echo $TASK | python3 -c "import sys,json; print(json.load(sys.stdin)['task_id'])")
echo "task_id=$TASK_ID"

# 2. 轮询等待
while true; do
  STATUS=$(curl -s "http://10.60.151.130:7107/api/status?task_id=$TASK_ID")
  echo $STATUS | python3 -c "import sys,json; print(json.load(sys.stdin)['progress'])"
  if echo $STATUS | grep -q "finished\|failed"; then break; fi
  sleep 2
done

# 3. 获取结果（含 links + results + summary）
curl -s "http://10.60.151.130:7107/api/results?task_id=$TASK_ID" | python3 -m json.tool | head -50
```

### Python

```python
import time
import requests

BASE = "http://10.60.151.130:7107"

# 1. 启动
r = requests.post(f"{BASE}/api/crawl", json={"url": "https://szs.mof.gov.cn/zhengcefabu/"})
task_id = r.json()["task_id"]

# 2. 轮询
while True:
    r = requests.get(f"{BASE}/api/status", params={"task_id": task_id})
    data = r.json()
    print(data["progress"])
    if data["status"] in ("finished", "failed"):
        break
    time.sleep(2)

# 3. 获取结果
r = requests.get(f"{BASE}/api/results", params={"task_id": task_id})
results = r.json()
print(f"文章数: {len(results['results'])}")
print(f"摘要: {results['summary']}")
```

---

## 项目结构

```
gov_crawler/
├── crawler.py            # 核心：WaterCrawl 客户端 + AI 识别/清洗/摘要 + TaskManager + Worker
├── main.py               # FastAPI 入口（5 个端点）
├── Dockerfile            # Python 3.12-slim
├── docker-compose.yml    # gov-crawler + gov-redis
├── requirements.txt      # fastapi / uvicorn / httpx / bs4 / lxml / redis
├── .env.example          # 环境变量模板
├── .env                  # 实际配置（gitignore）
├── static/
│   └── test.html         # 前端测试页面
├── API.md                # API 详细文档
└── README.md             # 本文件
```
