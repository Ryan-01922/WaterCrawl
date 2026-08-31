# gov_crawler 交接文档

> 政府网站智能爬虫服务：爬取政府/新闻站点列表页 → AI 识别文章链接 → 批量爬取正文 → AI 生成每篇摘要与全局摘要 → 按日期排序输出。

## 1. 系统架构

```
用户/前端
  │  POST /api/crawl
  ▼
gov-crawler (FastAPI, 端口 7106)
  │  ① 调 WaterCrawl 爬列表页 (Layer 1)
  │  ② AI(Qwen3-32B) 从 DOM 树识别文章链接
  │  ③ robots.txt 预检（合规）
  │  ④ 调 WaterCrawl 批量爬文章正文 (Layer 2)
  │  ⑤ AI 分批清洗每篇文章（TTL/DTM/SRC/ABS）
  │  ⑥ 按 publish_date 降序排序，取最新 30 篇
  │  ⑦ AI 生成全局摘要
  ▼
Redis (宿主机 16379, 24h TTL)
  │
  ├─ GET /api/status?task_id=xxx   轮询任务状态
  └─ GET /api/results?task_id=xxx  获取完整结果
```

**依赖外部服务**：
- **WaterCrawl**（Django，端口 7109）：通用爬取引擎，负责实际抓网页
- **GPUStack + Qwen3-32B**：AI 识别/清洗/摘要
- **Redis**（宿主机 16379）：任务队列 + 结果存储

## 2. 部署环境

| 项 | 值 |
|----|-----|
| 服务器 | 10.206.216.100（新）/ 10.60.207.83（旧测试） |
| 代码路径 | /data/cfzqoper/mbrtest/WaterCrawl/gov_crawler |
| 服务端口 | 7106（内部 7106，已从 7107 改过） |
| 容器名 | gov-crawler |
| Worker 数 | 3 |

## 3. 配置文件（.env）

```ini
# WaterCrawl 配置（重要：必须显式配置，代码默认值指向旧服务器）
WATERCRAWL_BASE_URL=http://<WaterCrawl服务器IP>:7109/api/v1/core
WATERCRAWL_API_KEY=<纯key，不带Bearer前缀>

# GPUStack 大模型
GPUSTACK_API_BASE=https://gpustack.stock.hnchasing.com/v1
GPUSTACK_API_KEY=<key>
GPUSTACK_MODEL=qwen3-32b

# Redis
REDIS_HOST=host.docker.internal
REDIS_PORT=16379
REDIS_PASSWORD=mypassword123
```

**注意**：
- WaterCrawl 鉴权用 `X-API-Key` 头，**不要加 Bearer 前缀**，否则 401
- `.env` 不入 git，换服务器必须重新配
- 代码中 `WATERCRAWL_BASE_URL` 默认值是写死的旧 IP，不配 `.env` 会连错

## 4. 启动 / 更新

```bash
# 首次启动或代码更新后（代码打进镜像，必须 rebuild）
cd /data/cfzqoper/mbrtest/WaterCrawl/gov_crawler
docker compose up -d --build

# 查看日志
docker logs --tail 200 -f gov-crawler

# 停止/重启
docker compose down
docker compose up -d
```

## 5. API 接口

| 接口 | 说明 |
|------|------|
| `POST /api/crawl` | 入参 `{"url":"https://..."}`，返回 `{task_id}` |
| `GET /api/status?task_id=xxx` | 状态：pending/running/finished/failed + progress |
| `GET /api/results?task_id=xxx` | 完整结果（下方结构） |
| `GET /api/sources` | 预设站点列表 |

**results 返回结构**：

```json
{
  "task_id": "xxx",
  "status": "finished",
  "progress": "全部完成: 30 篇文章 + AI 摘要",
  "total": 30,
  "links": [{ "title": "标题", "url": "https://..." }],
  "results": [
    {
      "title": "列表页标题",
      "url": "https://...",
      "cleaned": {
        "title": "完整标题",
        "publish_date": "2026-08-14",
        "source": "新华网",
        "body": "AI 摘要"
      }
    }
  ],
  "summary": "全局摘要"
}
```

**cleaned.body 失败标记**：
- `页面反爬，robots.txt 禁止访问` — robots.txt 拦截
- `链接内容爬取失败` — WaterCrawl 爬不到（超时/丢失/反爬）

## 6. 代码结构（crawler.py 约 1060 行）

| 区块 | 行号 | 功能 |
|------|------|------|
| 配置区 | 29-99 | 环境变量、预设站点、URL 过滤正则 |
| WaterCrawl 客户端 | 110-212 | 调 WaterCrawl API（创建/轮询/结果/批量） |
| 链接提取 | 215-529 | 三阶段 AI 识别文章链接 |
| robots.txt 预检 | 532-557 | 合规检查（带缓存） |
| 批量爬取 | 560-609 | 爬文章详情页 |
| AI 清洗+摘要 | 612-793 | 每篇摘要 + 全局摘要 |
| Redis 任务管理 | 796-870 | TaskManager |
| Worker 进程 | 873-1057 | 3 个 worker 取任务执行 |

### 核心流程关键点

**1. AI 识别文章链接（阶段2）**
`_build_dom_tree_text`（248行）把 HTML 转成带行号的缩进树 → `ai_extract_article_links`（347行）让 AI 只返回文章行号数组 `[12,35,48]` → 按行号还原 title/url。AI 失败走 `fallback_extract_article_links`（424行）三阶梯规则。

**2. 正文提取**
`_extract_raw_text`（约605行）优先从 HTML 提取 `<p>` 正文（跳过 nav/header/footer/script），不用 markdown——因为 WaterCrawl 的 markdown 开头有大量导航，直接截断会导致清洗只看到导航。

**3. 批量爬取参数**
- `wait_time: 5000`（等 JS 渲染）
- `timeout: 60000`
- `only_main_content: False`（True 会误伤正文被过滤掉）
- `include_html: True`（清洗可兜底）

**4. 排序**
清洗完成后按 `publish_date` 降序排序（无日期排最后），截取最新 30 篇输出。

## 7. 常见问题排查

### 7.1 后几篇没有摘要
原因：旧版一次性把所有文章丢给 AI，`max_tokens` 不够输出被截断。
现状：已改为分批（每批 10 篇），每批独立调用，无此问题。

### 7.2 清洗结果是"导航菜单/无标题"
原因（已修）：
1. `only_main_content: True` 把正文过滤掉了 → 改为 False
2. `_extract_raw_text` 取 markdown 前 5000 字符，开头全是导航 → 改为从 HTML 提取 `<p>` 正文

### 7.3 `Unterminated string` JSON 解析错误
原因：AI 输出超过 max_tokens 被截断。
修复：`max_tokens` 提到 8192 + `_parse_line_numbers` 三级解析兜底 + 强化 prompt。

### 7.4 `KeyError: 'title'`
原因：阶段2 link_map 用 `text` 字段，降级方案用 `title` 字段，结构不一致。
修复：阶段2 返回前统一转 `{"title", "url"}`。

### 7.5 WaterCrawl 丢失部分 URL
表现：`WaterCrawl 批量爬取丢失 N/30 篇`。
处理：按 URL 匹配（`result_by_url`/`cleaned_by_url`）而非按索引，避免错位；丢失的标记 `链接内容爬取失败`。
排查方向：目标站反爬、超时、robots.txt。

### 7.6 robots.txt
`SiteScrapper`（WaterCrawl 批量爬取用的 Spider）默认遵守 robots.txt，被禁的 URL 直接丢弃。gov_crawler 有预检逻辑，被禁的标记 `页面反爬，robots.txt 禁止访问`。

### 7.7 调 WaterCrawl 401
确认：
1. `WATERCRAWL_API_KEY` 是纯 key（无 Bearer）
2. WaterCrawl 已启动且 API Key 有效
3. 端口 7109 可达

## 8. 新增站点接入

在 `PRESET_SOURCES`（crawler.py 约 55 行）添加：

```python
{
    "key": "site_key",
    "label": "站点名 - 栏目名",
    "url": "https://xxx/index.htm",
    "desc": "站点 / 栏目说明",
},
```

注意：列表页结构不同，AI 识别失败会自动走规则降级（URL 白名单 → li a → 全页面启发式）。

## 9. 注意事项

- 结果存 Redis 24h TTL，超时后需重新爬取
- Worker 异常会自动重启（FIRST_EXCEPTION 机制）
- 修改代码后**必须 rebuild**（镜像 COPY 代码，非挂载卷）
- 生产服务器用 SSH：`ssh cfzqoper@10.206.216.100`
