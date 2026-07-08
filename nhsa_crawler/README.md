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

| 端点 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 访问测试页面 |
| `/api/crawl` | POST | 启动爬取任务（爬取全部三个栏目） |
| `/api/status` | GET | 查询当前任务状态和进度 |
| `/api/results` | GET | 获取爬取结果（全部栏目） |
| `/api/results?section=yibao_zhengce` | GET | 获取指定栏目结果 |

### 栏目名称对照

| 参数值 | 栏目 |
|--------|------|
| `yibao_zhengce` | 医保政策 |
| `dongtai` | 动态 |
| `tongji_shuju` | 统计数据 |

### 调用示例

```bash
# 启动爬取
curl -X POST http://10.60.151.130:7108/api/crawl

# 查询状态
curl http://10.60.151.130:7108/api/status

# 获取结果
curl http://10.60.151.130:7108/api/results
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

```
首页 https://www.nhsa.gov.cn/
    │
    ├── 解析出三个栏目的链接（基于关键词匹配）
    │
    ├── 医保政策列表页 ── 提取文章链接 ── 批量爬取文章内容
    ├── 动态列表页    ── 提取文章链接 ── 批量爬取文章内容
    └── 统计数据列表页 ── 提取文章链接 ── 批量爬取文章内容
```

每个列表页只爬取 **第 1 页**。

---

## 注意事项

1. **API Key 安全**：不要在代码中硬编码 API Key，使用环境变量传入
2. **爬取频率**：WaterCrawl 已有下载延迟等控制，无需额外限流
3. **超时设置**：批量爬取文章的超时时间较长为 600 秒，请耐心等待
4. **文章数量**：各栏目首页列表的文章数量可能不同，测试页面会显示实时的文章计数
