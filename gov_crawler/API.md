# Gov Crawler API 文档

## 基础信息

| 项目 | 说明 |
|------|------|
| 基础 URL | `http://10.60.151.130:7107` |
| 数据格式 | 请求和响应均为 JSON |
| 字符编码 | UTF-8 |

---

## 接口列表

| 端点 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 测试页面 |
| `/api/sources` | GET | 获取预设站点列表 |
| `/api/crawl` | POST | 启动爬取 |
| `/api/status` | GET | 查询状态 |
| `/api/results` | GET | 获取结果 + AI 摘要 |

---

### 1. GET /api/sources

获取预设的政府网站列表。

**响应示例：**

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

---

### 2. POST /api/crawl

启动爬取任务。传入目标列表页 URL，异步执行，立即返回。

**请求体：**

```json
{
  "url": "https://szs.mof.gov.cn/zhengcefabu/"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `url` | string | 是 | 目标网站列表页完整 URL，必须以 `http://` 或 `https://` 开头 |

**响应示例（成功）：**

```json
{
  "status": "started",
  "message": "爬取任务已启动",
  "url": "https://szs.mof.gov.cn/zhengcefabu/"
}
```

**响应示例（任务已在执行）：** HTTP 409

```json
{
  "status": "busy",
  "message": "爬取任务正在执行中，请等待完成"
}
```

**调用示例：**

```bash
# 使用预设站点
curl -X POST http://10.60.151.130:7107/api/crawl \
  -H "Content-Type: application/json" \
  -d '{"url": "https://szs.mof.gov.cn/zhengcefabu/"}'

# 使用自定义站点
curl -X POST http://10.60.151.130:7107/api/crawl \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.mohrss.gov.cn/xxx/"}'
```

---

### 3. GET /api/status

查询当前爬取任务的运行状态和进度。

**响应示例：**

```json
{
  "status": "running",
  "progress": "Layer 2: 正在批量爬取 12 篇文章..."
}
```

**status 枚举：**

| 值 | 说明 |
|----|------|
| `idle` | 空闲，无任务执行 |
| `running` | 爬取中 |
| `finished` | 已完成 |
| `failed` | 失败 |

**progress 典型值：**

| 阶段 | 内容 |
|------|------|
| Layer 1 | `Layer 1: 正在爬取列表页...` |
| Layer 1 | `Layer 1: AI 正在识别文章链接...` |
| Layer 1 | `Layer 1 完成: 识别到 12 篇文章` |
| Layer 2 | `Layer 2: 正在批量爬取 12 篇文章...` |
| 摘要 | `正在生成 AI 摘要...` |
| 完成 | `全部完成: 12 篇文章 + AI 摘要` |
| 失败 | `列表页爬取失败 (status=failed, html_len=0)` |

**调用示例：**

```bash
curl http://10.60.151.130:7107/api/status
```

---

### 4. GET /api/results

获取爬取结果和 AI 生成的摘要。

**响应示例：**

```json
{
  "status": "finished",
  "progress": "全部完成: 12 篇文章 + AI 摘要",
  "results": [
    {
      "title": "关于2027年第33届世界大学生冬季运动会税收政策的通知",
      "url": "https://szs.mof.gov.cn/zhengcefabu/202607/t20260708_3993182.htm",
      "content": {
        "uuid": "xxx-xxx-xxx",
        "url": "https://szs.mof.gov.cn/...",
        "result": {
          "markdown": "## 关于2027年第33届世界大学生冬季运动会税收政策的通知\n\n...",
          "html": "<h2>关于2027年...</h2>",
          "metadata": {}
        }
      }
    }
  ],
  "summary": "本批12篇文章主要涵盖三大政策方向：一是新能源汽车车船税优惠政策调整，延续绿色出行扶持导向；二是增值税预缴与进项税额抵扣细则出台..."
}
```

**结果字段说明：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `status` | string | 任务状态 |
| `progress` | string | 进度描述 |
| `results` | array | 文章列表 |
| `results[].title` | string | 文章标题 |
| `results[].url` | string | 文章原始 URL |
| `results[].content.uuid` | string | WaterCrawl 任务 UUID |
| `results[].content.result.markdown` | string | 文章 Markdown 内容 |
| `results[].content.result.html` | string | 文章 HTML 内容 |
| `summary` | string | AI 基于所有标题生成的摘要 |

**进度中返回示例：**

```json
{
  "status": "running",
  "progress": "Layer 2: 正在批量爬取 12 篇文章...",
  "results": null,
  "summary": null
}
```

**调用示例：**

```bash
# 获取完整结果
curl http://10.60.151.130:7107/api/results | jq .

# 仅查看摘要
curl -s http://10.60.151.130:7107/api/results | jq '.summary'

# 仅查看文章数量
curl -s http://10.60.151.130:7107/api/results | jq '.results | length'
```

---

## 错误码

| HTTP 状态码 | 说明 |
|-------------|------|
| 200 | 成功 |
| 400 | 请求参数错误（如 URL 为空或不是 http 开头） |
| 409 | 资源冲突（已有任务在执行） |
| 500 | 服务器内部错误 |

---

## 完整调用流程

```bash
# 1. 查看可用站点
curl http://10.60.151.130:7107/api/sources

# 2. 启动爬取
curl -X POST http://10.60.151.130:7107/api/crawl \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.cac.gov.cn/wxzw/wxfb/A093702index_1.htm"}'

# 3. 轮询状态（每 3 秒一次，直到 finished）
curl http://10.60.151.130:7107/api/status

# 4. 获取结果 + 摘要
curl http://10.60.151.130:7107/api/results | jq .
```

---

## Python 调用示例

```python
import time
import httpx

BASE = "http://10.60.151.130:7107"

# 启动
resp = httpx.post(f"{BASE}/api/crawl", json={"url": "https://szs.mof.gov.cn/zhengcefabu/"})
print(resp.json())

# 轮询
while True:
    s = httpx.get(f"{BASE}/api/status").json()
    print(s["progress"])
    if s["status"] in ("finished", "failed"):
        break
    time.sleep(3)

# 获取结果
data = httpx.get(f"{BASE}/api/results").json()
print(f"文章数: {len(data['results'])}")
print(f"摘要: {data['summary']}")
```

---

## 爬取架构

```
POST /api/crawl (异步)
        │
        ▼
┌─────────────────────────────────────┐
│  Layer 1: 爬取列表页                  │
│  WaterCrawl → HTML                  │
│  Qwen3-32B 智能筛选文章链接            │
│  AI 失败 → 降级到规则匹配               │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│  Layer 2: 批量爬取文章                 │
│  WaterCrawl batch API               │
│  获取 markdown/html 内容              │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│  AI 摘要: Qwen3-32B 读取所有标题       │
│  生成 200 字内容概括                   │
└─────────────────────────────────────┘
               │
               ▼
         返回 results + summary
```
