# NHSA Crawler - 国家医保局网站内容爬取服务

基于 WaterCrawl API 的爬虫服务，自动爬取 [国家医保局官网](https://www.nhsa.gov.cn/) 三个栏目的内容：

- **医保政策** — 医保政策相关文件
- **动态** — 新闻动态、工作动态
- **统计数据** — 统计信息、统计数据

---

## 目录结构

```
nhsa_crawler/
├── static/
│   └── test.html          # Web 测试页面
├── crawler.py             # 核心爬取逻辑（调用 WaterCrawl API）
├── main.py                # FastAPI 应用入口
├── Dockerfile             # Docker 镜像构建
├── docker-compose.yml     # Docker Compose 编排
└── requirements.txt       # Python 依赖
```

---

## 前置条件

1. WaterCrawl 服务已部署并正常运行（默认地址 `http://10.60.151.130:7109`）
2. 已获取 WaterCrawl API Key（在 WaterCrawl 管理后台的 API Keys 页面创建）
3. 服务器已安装 Docker 和 Docker Compose

---

## 本地开发

### 1. 安装依赖

```bash
cd nhsa_crawler
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
# Windows (PowerShell)
$env:WATERCRAWL_BASE_URL="http://10.60.151.130:7109/api/v1/core"
$env:WATERCRAWL_API_KEY="your_api_key_here"

# Linux / macOS
export WATERCRAWL_BASE_URL="http://10.60.151.130:7109/api/v1/core"
export WATERCRAWL_API_KEY="your_api_key_here"
```

### 3. 启动服务

```bash
python main.py
```

服务默认监听 `http://0.0.0.0:7108`，浏览器打开 `http://localhost:7108` 访问测试页面。

---

## Docker 部署

### 1. 构建和启动

```bash
# 在 WaterCrawl 项目根目录执行
docker-compose -f nhsa_crawler/docker-compose.yml up -d --build
```

### 2. 查看日志

```bash
docker logs -f nhsa-crawler
```

### 3. 停止服务

```bash
docker-compose -f nhsa_crawler/docker-compose.yml down
```

### 4. 配置 API Key

编辑 `nhsa_crawler/docker-compose.yml`，修改环境变量：

```yaml
environment:
  - WATERCRAWL_API_KEY=your_api_key_here
```

或者通过 `.env` 文件传入（推荐）：

```bash
# 在 nhsa_crawler/ 目录下创建 .env 文件
echo "WATERCRAWL_API_KEY=your_api_key_here" > .env

# 然后启动
docker-compose -f nhsa_crawler/docker-compose.yml up -d
```

---

## 推送到服务器并运行

### 1. 本地提交代码

```bash
git add nhsa_crawler/
git commit -m "feat: add nhsa crawler service"
git push
```

### 2. 服务器拉取并启动

```bash
# 登录服务器后
cd /path/to/WaterCrawl
git pull

# 启动服务
export WATERCRAWL_API_KEY="your_api_key_here"
docker-compose -f nhsa_crawler/docker-compose.yml up -d --build

# 验证运行状态
docker ps | grep nhsa-crawler
docker logs nhsa-crawler
```

---

## API 接口说明

### 基础信息

| 项目 | 说明 |
|------|------|
| 基础 URL | `http://10.60.151.130:7108` |
| 数据格式 | 全部请求和响应均为 JSON |
| 字符编码 | UTF-8 |

---

### 1. 测试页面

```
GET /
```

返回测试页面 HTML，浏览器直接访问即可。

---

### 2. 启动爬取

```
POST /api/crawl
```

启动一次完整的爬取流程（三层爬取），异步执行，立即返回。

**响应示例（成功）：**
```json
{
  "status": "started",
  "message": "爬取任务已启动"
}
```

**响应示例（已有任务在执行）：** (HTTP 409)
```json
{
  "status": "busy",
  "message": "爬取任务正在执行中，请等待完成"
}
```

---

### 3. 查询状态

```
GET /api/status
```

查询当前爬取任务的运行状态和进度信息。

**响应示例：**
```json
{
  "status": "running",
  "progress": "Layer 2: 正在爬取【医保政策】列表页..."
}
```

**status 枚举：**

| 值 | 说明 |
|----|------|
| `idle` | 空闲，无任务执行 |
| `running` | 爬取任务执行中 |
| `finished` | 爬取完成 |
| `failed` | 爬取出错 |

**progress 示例：**

| 阶段 | progress 内容 |
|------|--------------|
| Layer 1 | `Layer 1: 正在爬取首页...` |
| Layer 1 | `Layer 1: AI 正在识别栏目链接...` |
| Layer 1 | `Layer 1 完成: 识别到 3 个栏目链接` |
| Layer 2 | `Layer 2: 正在爬取【医保政策】列表页...` |
| Layer 2 | `【医保动态】列表页找到 15 篇文章` |
| Layer 3 | `Layer 3: 正在批量爬取【医保政策】15 篇文章...` |
| 完成 | `爬取全部完成！` |
| 失败 | `爬取出错: {错误详情}` |

---

### 4. 获取结果

```
GET /api/results
GET /api/results?section={栏目名称}
```

获取爬取结果数据。

**查询参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `section` | string | 否 | 栏目名称，不传则返回全部栏目 |

**section 可选值：**

| 值 | 栏目 | 对应网站栏目 |
|----|------|-------------|
| `yibao_zhengce` | 医保政策 | 政策法规 (col104) |
| `dongtai` | 动态 | 医保动态 (col14) |
| `tongji_shuju` | 统计数据 | 统计数据 (col7) |

**响应示例（全部结果）：**
```json
{
  "status": "finished",
  "progress": "爬取全部完成！",
  "results": {
    "yibao_zhengce": [
      {
        "title": "关于印发2026年纠正医药购销领域和医疗服务中不正之风工作要点的通知",
        "url": "https://www.nhsa.gov.cn/art/2026/6/9/art_104_20892.html",
        "content": {
          "uuid": "...",
          "url": "https://www.nhsa.gov.cn/...",
          "result": {
            "markdown": "## 文章内容...",
            "html": "<h2>文章内容...</h2>",
            "metadata": {...}
          }
        }
      }
    ],
    "dongtai": [...],
    "tongji_shuju": [...]
  }
}
```

**响应示例（指定栏目）：**
```json
{
  "status": "finished",
  "progress": "爬取全部完成！",
  "results": [
    {
      "title": "2026年1-5月基本医疗保险统筹基金和生育保险主要指标",
      "url": "https://www.nhsa.gov.cn/art/2026/6/12/art_7_20975.html",
      "content": {
        "uuid": "...",
        "url": "https://www.nhsa.gov.cn/...",
        "result": {
          "markdown": "## 2026年1-5月...",
          "html": "...",
          "metadata": {...}
        }
      }
    }
  ]
}
```

**响应示例（任务未完成）：**
```json
{
  "status": "running",
  "progress": "Layer 2: 正在爬取【医保动态】列表页...",
  "results": null
}
```

---

### 5. 错误码说明

| HTTP 状态码 | 说明 |
|-------------|------|
| 200 | 请求成功 |
| 400 | 参数错误（如无效的 section 名称） |
| 409 | 资源冲突（爬取任务已在执行中） |
| 500 | 服务器内部错误 |

---

### 调用示例

```bash
# 1. 启动爬取任务
curl -X POST http://10.60.151.130:7108/api/crawl
# 响应: {"status":"started","message":"爬取任务已启动"}

# 2. 轮询查询状态（建议间隔 2-3 秒）
curl http://10.60.151.130:7108/api/status
# 响应: {"status":"running","progress":"Layer 2: 正在爬取【医保动态】列表页..."}

# 3. 爬取完成后获取全部结果
curl http://10.60.151.130:7108/api/results | jq .

# 4. 获取指定栏目结果
curl "http://10.60.151.130:7108/api/results?section=dongtai" | jq .
```

---

## 测试页面

启动服务后，浏览器访问 `http://10.60.151.130:7108` 即可打开测试页面。

页面功能：
- 查看当前 WaterCrawl 配置
- 一键启动爬取（按钮）
- 实时查看爬取进度和状态
- 按栏目切换查看结果列表
- 展开/收起文章内容预览

---

## 端口说明

| 服务 | 端口 | 说明 |
|------|------|------|
| NHSA Crawler | **7108** | 本服务 API + 测试页面 |
| WaterCrawl | **7109** | WaterCrawl API 服务 |

---

## 爬取流程

采用三层架构，逐层深入：

```
Layer 1: 爬取首页
    │
    ├── WaterCrawl 爬取首页 HTML
    ├── Qwen3-32B (GPUStack) AI 识别三个栏目的链接
    │   └── 失败时降级到关键词匹配
    │
    ▼
Layer 2: 爬取栏目列表页（第 1 页）
    │
    ├── 医保政策 (col104) ── 通过 /art/ URL 模式提取文章链接
    ├── 医保动态 (col14)  ── 通过 /art/ URL 模式提取文章链接
    └── 统计数据 (col7)   ── 通过 /art/ URL 模式提取文章链接
    │
    ▼
Layer 3: 批量爬取文章详情
    │
    ├── WaterCrawl batch API 并发爬取所有文章
    ├── 获取文章 markdown/html 内容
    └── 按栏目归类存储
```

### 栏目与网站真实映射

| 爬取栏目 | 网站原始栏目 | 栏目 ID |
|---------|-------------|---------|
| 医保政策 | 政策法规 | col104 |
| 动态 | 医保动态 | col14 |
| 统计数据 | 统计数据 | col7 |

### 文章 URL 模式

所有文章链接均符合以下模式：
```
https://www.nhsa.gov.cn/art/{年份}/{月}/art_{栏目ID}_{文章ID}.html
```

示例：
- `https://www.nhsa.gov.cn/art/2026/7/9/art_14_21343.html` — 医保动态文章
- `https://www.nhsa.gov.cn/art/2026/6/9/art_104_20892.html` — 政策法规文章
- `https://www.nhsa.gov.cn/art/2026/6/12/art_7_20975.html` — 统计数据文章

---

## 注意事项

1. **API Key 安全**：不要在代码中硬编码 API Key，使用环境变量传入
2. **爬取频率**：WaterCrawl 已有下载延迟等控制，无需额外限流
3. **超时设置**：批量爬取文章的超时时间较长为 600 秒，请耐心等待
4. **文章数量**：各栏目首页列表的文章数量可能不同，测试页面会显示实时的文章计数
