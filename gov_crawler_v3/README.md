# Gov Crawler — 通用政府网站文章爬取服务

基于 WaterCrawl API + Qwen3-32B AI + Redis 任务队列，实现**任意政府网站列表页**的两层自动化爬取：列表页 → AI 识别文章链接 → 批量爬取正文 → AI 生成每篇摘要与全局摘要 → 按日期排序输出最新 30 篇。

---

## 目录

- [架构概览](#架构概览)
- [完整 Pipeline](#完整-pipeline)
- [API 文档](#api-文档)
- [环境变量](#环境变量)
- [部署](#部署)
- [调用示例](#调用示例)
- [项目结构](#项目结构)
- [注意事项](#注意事项)

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
| **通用性** | 不依赖固定网站结构，AI 基于 DOM 结构树自动识别文章链接 |
| **多级降级** | AI → URL 白名单正则 → `li a` 选择器 → 全页面启发式 |
| **任务队列** | Redis + 3 Worker，支持并发提交，互不阻塞 |
| **iframe 适配** | 自动检测并追加爬取 iframe/frame 内容 |
| **内容清洗** | AI 分批提取 title/date/source/body，去除导航和页脚 |
| **智能摘要** | AI 基于文章标题生成约 200 字全局摘要 |
| **30 篇上限** | 清洗完成后按发布日期降序排序，截取最新 30 篇输出 |

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
│      直到 status=finished（最长 120s）               │
│                                                      │
│  2c. GET /crawl-requests/{uuid}/results/             │
│      → result 字段是 MinIO 预签名 URL                │
│      → 替换 localhost 后下载 JSON → 拿到 HTML        │
└─────────────────────────────────────────────────────┘
        │
        ▼
┌─ Step 3: iframe 检测 ───────────────────────────────┐
│  3a. BeautifulSoup 解析 HTML                         │
│  3b. find_all("iframe"/"frame", src=True)            │
│      ├── 找到 iframe → urljoin 拼出完整 URL           │
│      │   → 逐个爬取并将 HTML 合并到主 HTML           │
│      └── 没找到 → 跳过，继续下一步                    │
└─────────────────────────────────────────────────────┘
        │
        ▼
┌─ Step 4: AI 识别文章链接（多层降级） ────────────────┐
│  4a. 把 DOM 转成带行号标记的缩进树文本               │
│      每个链接行形如: <a L12> 标题                    │
│  4b. 将树文本发给 Qwen3-32B                          │
│      Prompt: "根据页面结构判断哪些链接是文章链接"     │
│      → AI 只返回行号数组 [12, 35, 48]                │
│  4c. 按行号还原 title/url                            │
│                                                      │
│  AI 失败/结果 < 3 篇时启用降级：                      │
│  3a. URL 白名单正则匹配                              │
│      /art/ | /t{日期}_{编号}.htm | /c_{编号}.htm      │
│      | /content/ | /info/ | /xxgk/ 等 10 种           │
│  3b. <li> <a href> 通用选择器                        │
│  3c. 全页面链接 + 启发式过滤                          │
│      （排除分页/导航/纯数字/栏目首页）                │
│                                                      │
│  合并策略:                                           │
│  • AI 返回 >= 3 篇 → 仅用 AI 结果                    │
│  • AI 返回 1-2 篇 → 合并 AI+降级（取并集去重）       │
│  • AI 返回 0 篇 → 全部用降级结果                     │
└─────────────────────────────────────────────────────┘
        │
        ▼  articles_info = [{title, url}, ...]
        │
┌─ Step 5: robots.txt 预检 ───────────────────────────┐
│  逐个 URL 检查 robots.txt（带域名级缓存）            │
│  ├── 允许 → 进入批量爬取                             │
│  └── 禁止 → 标记 "页面反爬，robots.txt 禁止访问"     │
└─────────────────────────────────────────────────────┘
        │
        ▼
┌─ Step 6: Layer 2 — 批量爬取文章 ───────────────────┐
│  6a. WaterCrawl POST /crawl-requests/batch/          │
│      Body: {"urls": [...], "options": {               │
│        "include_html": true,                         │
│        "only_main_content": false,  ← 防正文被误过滤 │
│        "wait_time": 5000,          ← 等 JS 渲染     │
│        "timeout": 60000                              │
│      }}                                             │
│                                                      │
│  6b. 轮询等待 batch 任务完成（最长 600s）             │
│  6c. 下载每篇文章的 MinIO 结果文件                    │
│  6d. 丢失的文章标记 "链接内容爬取失败"                │
└─────────────────────────────────────────────────────┘
        │
        ▼
┌─ Step 7: AI 内容清洗 ───────────────────────────────┐
│  7a. 每篇截取前 5000 字符正文                        │
│  7b. 分批（每批 10 篇）用 ===== 分隔拼接后发给 AI     │
│                                                      │
│  Prompt: "为每篇文章生成 200 字以内的摘要"            │
│  返回 TTL/DTM/SRC/ABS 分隔格式                        │
│  解析：split("===") → 前缀匹配                        │
│                                                      │
│  cleaned = [{title, publish_date, source, body}, ...]│
│  (body 字段存储摘要内容)                              │
└─────────────────────────────────────────────────────┘
        │
        ▼
┌─ Step 8: 排序截断 ──────────────────────────────────┐
│  按 publish_date 降序排序（无日期排最后）             │
│  截取最新 30 篇（links 与 results 保持一致）          │
└─────────────────────────────────────────────────────┘
        │
        ▼
┌─ Step 9: AI 全局摘要 ───────────────────────────────┐
│  收集所有清洗后的标题 → 发给 Qwen3-32B               │
│  Prompt: "根据以下 N 篇政府政策文章标题，             │
│           写一个 200 字以内摘要"                     │
│  返回一段概括性文字                                   │
└─────────────────────────────────────────────────────┘
        │
        ▼
┌─ Step 10: 存储结果 ─────────────────────────────────┐
│  Redis Hash: gov_crawler:task:{task_id}              │
│  ├── links: JSON [{title, url}, ...]  最新 30 篇     │
│  ├── results: JSON [{title, url, content, cleaned}]  │
│  ├── summary: 摘要文本                               │
│  ├── status: "finished"                              │
│  └── progress: "全部完成: 30 篇文章 + AI 摘要"        │
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
    {
      "key": "caizhengbu",
      "label": "财政部税政司 - 政策发布",
      "url": "https://szs.mof.gov.cn/zhengcefabu/",
      "desc": "财政部税政司 / 政策发布栏目"
    },
    {
      "key": "cac",
      "label": "中央网信办 - 网信发布",
      "url": "https://www.cac.gov.cn/wxzw/wxfb/A093702index_1.htm",
      "desc": "中央网信办 / 网信政务 / 网信发布栏目"
    }
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
{"status": "queued", "message": "爬取任务已加入队列", "task_id": "a1b2c3d4", "url": "..."}
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
  "total": 10,
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

> `links` 与 `results` 数量一致，均为按发布日期排序后的最新 30 篇（不足 30 篇则全量）。

**cleaned.body 失败标记**：

- `页面反爬，robots.txt 禁止访问` — 被 robots.txt 拦截
- `链接内容爬取失败` — WaterCrawl 爬不到（超时/丢失/反爬）

---

## 环境变量

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `WATERCRAWL_API_KEY` | **是** | — | WaterCrawl API 密钥（纯 key，不带 Bearer 前缀） |
| `WATERCRAWL_BASE_URL` | 否 | `http://10.60.151.130:7109/api/v1/core` | WaterCrawl API 地址（必须显式配置，代码默认指向旧服务器） |
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
- WaterCrawl 服务已在目标地址:7109 运行
- GPUStack API 已就绪
- 服务器上已有 Redis（端口 16379）

### 步骤

```bash
cd gov_crawler

# 1. 创建环境变量文件（.env 不入 git，换服务器必须重新配）
cp .env.example .env
nano .env  # 填入 WATERCRAWL_BASE_URL / WATERCRAWL_API_KEY / GPUSTACK_API_KEY

# 2. 启动（代码打进镜像，更新代码后必须 rebuild）
docker compose up -d --build

# 3. 验证
docker logs gov-crawler --tail 10
# 应看到: "已启动 3 个工作进程"
```

### 验证 Worker

```bash
docker logs gov-crawler | grep "Worker"
# Worker[0] 已启动
# Worker[1] 已启动
# Worker[2] 已启动
```

---

## 调用示例

### Bash

```bash
# 1. 启动爬取
TASK=$(curl -s -X POST http://<服务器IP>:7106/api/crawl \
  -H "Content-Type: application/json" \
  -d '{"url": "https://szs.mof.gov.cn/zhengcefabu/"}')
TASK_ID=$(echo $TASK | python3 -c "import sys,json; print(json.load(sys.stdin)['task_id'])")
echo "task_id=$TASK_ID"

# 2. 轮询等待
while true; do
  STATUS=$(curl -s "http://<服务器IP>:7106/api/status?task_id=$TASK_ID")
  echo $STATUS | python3 -c "import sys,json; print(json.load(sys.stdin)['progress'])"
  if echo $STATUS | grep -q "finished\|failed"; then break; fi
  sleep 2
done

# 3. 获取结果（含 links + results + summary）
curl -s "http://<服务器IP>:7106/api/results?task_id=$TASK_ID" | python3 -m json.tool | head -50
```

### Python

```python
import time
import requests

BASE = "http://<服务器IP>:7106"

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
├── docker-compose.yml    # gov-crawler（连接宿主机 Redis）
├── requirements.txt      # fastapi / uvicorn / httpx / bs4 / lxml / redis
├── .env.example          # 环境变量模板
├── .env                  # 实际配置（gitignore）
├── HANDOVER.md           # 交接文档（部署/排障，最新准确信息以它为准）
├── static/
│   └── test.html         # 前端测试页面
└── README.md             # 本文件
```

---

## 注意事项

- 结果存 Redis 24h TTL（`TASK_TTL=86400`），超时后需重新爬取
- Worker 异常会自动重启（`FIRST_EXCEPTION` 机制，检测到任一 Worker 退出后整体重启）
- 修改代码后**必须 rebuild**（镜像 COPY 代码，非挂载卷）
- WaterCrawl 鉴权用 `X-API-Key` 头，**不要加 Bearer 前缀**，否则 401
- 新增站点在 `crawler.py` 的 `PRESET_SOURCES` 中添加；列表页结构不同时 AI 识别失败会自动走规则降级
