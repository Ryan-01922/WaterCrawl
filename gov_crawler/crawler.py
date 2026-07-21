"""
政府网站列表页文章爬取服务 - 两层爬取架构
Layer 1: 列表页 → AI 识别文章链接
Layer 2: 文章详情页 → 批量爬取内容

完整复制 nhsa_crawler 方案，适用于任意政府网站列表页。
"""
import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin

import httpx
import redis.asyncio as aioredis
from bs4 import BeautifulSoup

logger = logging.getLogger("gov_crawler")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(handler)

# ---- 环境变量 ----
WATERCRAWL_BASE_URL = os.getenv(
    "WATERCRAWL_BASE_URL", "http://10.60.151.130:7109/api/v1/core"
)
WATERCRAWL_API_KEY = os.getenv("WATERCRAWL_API_KEY", "")

# 从 WaterCrawl base URL 提取主机地址，用于替换 MinIO result URL 中的 localhost
WATERCRAWL_HOST = WATERCRAWL_BASE_URL.replace("/api/v1/core", "").rstrip("/")

GPUSTACK_API_BASE = os.getenv(
    "GPUSTACK_API_BASE", "https://gpustack.stock.hnchasing.com/v1"
)
GPUSTACK_API_KEY = os.getenv("GPUSTACK_API_KEY", "")
GPUSTACK_MODEL = os.getenv("GPUSTACK_MODEL", "qwen3-32b")

# ---- Redis / Task Queue ----
_REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
_REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
_REDIS_DB = int(os.getenv("REDIS_DB", "0"))
_REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")
_REDIS_URL = os.getenv("REDIS_URL", "")
if not _REDIS_URL:
    if _REDIS_PASSWORD:
        _REDIS_URL = f"redis://:{_REDIS_PASSWORD}@{_REDIS_HOST}:{_REDIS_PORT}/{_REDIS_DB}"
    else:
        _REDIS_URL = f"redis://{_REDIS_HOST}:{_REDIS_PORT}/{_REDIS_DB}"
REDIS_URL = _REDIS_URL
WORKER_COUNT = int(os.getenv("WORKER_COUNT", "3"))
QUEUE_KEY = "gov_crawler:queue"
TASK_PREFIX = "gov_crawler:task:"
TASK_TTL = 86400  # 任务数据 24 小时后过期

# 预设站点
PRESET_SOURCES = [
    {
        "key": "caizhengbu",
        "label": "财政部税政司 - 政策发布",
        "url": "https://szs.mof.gov.cn/zhengcefabu/",
        "desc": "财政部税政司 / 政策发布栏目",
    },
    {
        "key": "cac",
        "label": "中央网信办 - 网信发布",
        "url": "https://www.cac.gov.cn/wxzw/wxfb/A093702index_1.htm",
        "desc": "中央网信办 / 网信政务 / 网信发布栏目",
    },
]

# 附件/下载类链接过滤（只过滤这些，其余全量给 AI）
ATTACHMENT_PATTERNS = [
    re.compile(r"\.docx?$", re.I),
    re.compile(r"\.xlsx?$", re.I),
    re.compile(r"\.pdf$", re.I),
    re.compile(r"\.zip$", re.I),
    re.compile(r"\.rar$", re.I),
    re.compile(r"\.pptx?$", re.I),
]

# 降级阶段的文章 URL 白名单（AI 失败时使用，只做正则匹配不做前置过滤）
ARTICLE_URL_PATTERNS = [
    re.compile(r"/art/", re.I),                         # nhsa 模式
    re.compile(r"/\d{6}/t\d{8}_\d+\.htm"),              # mof: /202607/t20260708_3993182.htm
    re.compile(r"/\d{4}-\d{2}/\d{2}/c_\d+\.htm"),       # cac: /2026-07/06/c_1785086223921593.htm
    re.compile(r"/content/\d+"),                         # 通用 content
    re.compile(r"/\d{4}-\d{2}/\d{2}/content_\d+"),       # 通用 content（日期格式）
    re.compile(r"/info/\d+"),                            # 通用 info
    re.compile(r"/xxgk/.*/\d+\.htm"),                   # 政务公开
    re.compile(r"/\d+/t\d+_\d+\.htm"),                  # 缩略日期: /2025/t12345_67890.htm
    re.compile(r"\.gov\.cn/.*/\d{4}.*\.html?"),         # 政府域名 + 年份标记
    re.compile(r"/\d{4}-\d{2}/\d{2}/.*\.htm"),          # 日期路径: /2025-06/18/xxx.htm
]


def _is_attachment_url(url: str) -> bool:
    """判断 URL 是否是附件/下载链接（这些不放行给 AI）"""
    for pat in ATTACHMENT_PATTERNS:
        if pat.search(url):
            return True
    return False


# ==================== WaterCrawl API 客户端 ====================

def _wc_headers() -> dict:
    """返回 headers，值使用 bytes 避免 httpx 的 ascii 编码问题"""
    return {"X-API-Key": WATERCRAWL_API_KEY.encode("utf-8"), "Content-Type": b"application/json"}


async def _create_crawl(client: httpx.AsyncClient, url: str, page_options: dict = None) -> str:
    logger.info("创建爬取任务: url=%s", url)
    options = {
        "spider_options": {"max_depth": 0, "page_limit": 1},
        "page_options": page_options or {
            "include_html": True,
            "only_main_content": False,
            "wait_time": 2000,
            "timeout": 30000,
        },
    }
    try:
        resp = await client.post(
            f"{WATERCRAWL_BASE_URL}/crawl-requests/",
            json={"url": url, "options": options},
            headers=_wc_headers(),
        )
        resp.raise_for_status()
        uuid = resp.json()["uuid"]
        logger.info("爬取任务创建成功: uuid=%s, url=%s", uuid, url)
        return uuid
    except Exception as e:
        logger.error("创建爬取任务失败: url=%s, error=%s", url, e)
        raise


async def _wait_crawl(client: httpx.AsyncClient, uuid: str, poll_interval: float = 2.0, max_wait: float = 120) -> dict:
    elapsed = 0.0
    while elapsed < max_wait:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
        resp = await client.get(
            f"{WATERCRAWL_BASE_URL}/crawl-requests/{uuid}/",
            headers=_wc_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        if data["status"] == "failed":
            logger.warning("爬取任务失败: uuid=%s, detail=%s", uuid, data.get("error", "无详情"))
        if data["status"] in ("finished", "failed", "canceled"):
            logger.info("爬取任务结束: uuid=%s, status=%s, 耗时=%.1fs", uuid, data["status"], elapsed)
            return data
    raise TimeoutError(f"爬取超时: {uuid}")


async def _get_results(client: httpx.AsyncClient, uuid: str) -> list[dict]:
    resp = await client.get(
        f"{WATERCRAWL_BASE_URL}/crawl-requests/{uuid}/results/",
        headers=_wc_headers(),
    )
    resp.raise_for_status()
    return resp.json().get("results", [])


async def _get_result_content(client: httpx.AsyncClient, result: dict) -> dict:
    """从 CrawlResult 中获取实际内容（处理 MinIO 内部 URL 问题）"""
    url = result.get("result", "")
    if url and isinstance(url, str) and url.startswith("http"):
        req_headers = {}
        if "localhost" in url:
            url = url.replace("http://localhost/", WATERCRAWL_HOST + "/")
            req_headers["Host"] = "localhost"
        try:
            r = await client.get(url, headers=req_headers)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.warning("下载结果文件失败: url=%s, error=%s", url[:80], e)
            return {}
    return result if isinstance(result, dict) else {}


async def _scrape_page(client: httpx.AsyncClient, url: str, page_options: dict = None) -> dict:
    """爬取单个页面，返回完整结果含 HTML 内容"""
    uuid = await _create_crawl(client, url, page_options)
    data = await _wait_crawl(client, uuid)
    if data.get("status") != "finished":
        return {"status": data.get("status"), "html": "", "results": []}
    results = await _get_results(client, uuid)
    if results:
        logger.info("爬取结果原始数据 keys=%s", list(results[0].keys()))
        result_val = results[0].get("result")
        logger.info("爬取结果 result 字段类型=%s, 值前200字符=%s", type(result_val).__name__, str(result_val)[:200])
    html = ""
    enriched = []
    for r in results:
        content = await _get_result_content(client, r)
        inner = content.get("result", content)
        if isinstance(inner, dict):
            h = inner.get("html", inner.get("markdown", ""))
            if h:
                html = h
        elif isinstance(inner, str):
            html = inner
        enriched.append({"uuid": r.get("uuid"), "url": r.get("url"), "result": inner})
    return {"status": "finished", "uuid": uuid, "html": html, "results": enriched}


# ==================== AI 文章链接提取 ====================

def _extract_links_from_html(html: str, base_url: str) -> list[dict]:
    """[阶段1] 从 HTML 中全量提取候选链接，只过滤 JS/锚点/附件，其余全部交给 AI"""
    soup = BeautifulSoup(html, "lxml")
    links = []
    seen = set()

    for tag in soup.select("a[href]"):
        text = tag.get_text(strip=True)
        href = tag.get("href", "").strip()
        # 基础过滤：空文本、JS、锚点、附件
        if not text or not href:
            continue
        if href.startswith("#") or href.startswith("javascript:") or href.startswith("mailto:"):
            continue
        full_url = urljoin(base_url, href)
        if full_url in seen:
            continue
        if _is_attachment_url(full_url):
            continue
        seen.add(full_url)
        # 提取父节点上下文
        parent_text = ""
        parent = tag.parent
        if parent:
            parent_text = parent.get_text(" ", strip=True)[:100]
        links.append({"text": text, "url": full_url, "context": parent_text})

    logger.info("[阶段1] 提取到 %d 个候选链接", len(links))
    return links


async def ai_extract_article_links(html: str, base_url: str) -> list[dict]:
    """[阶段2] 使用 Qwen3-32B 从全量候选链接中智能筛选文章链接"""
    all_links = _extract_links_from_html(html, base_url)
    if not all_links:
        logger.warning("[阶段2] HTML 中未提取到任何链接")
        return []

    # 截取前 400 个链接避免 prompt 过长
    ai_input = all_links[:400]
    links_text = json.dumps(ai_input, ensure_ascii=False, indent=2)

    prompt = f"""你是一个网页分析助手。以下是从一个政府网站的列表页提取到的所有链接。
请从中筛选出真正的「文章/正文」链接，排除以下：
- 导航栏（首页、上一页、下一页、尾页）
- 分页链接（页码数字）
- 面包屑路径
- 栏目首页、频道页
- 非正文页

要求：
- 每条返回 title 和 url
- 只返回 JSON 数组，不要任何其他文字

链接列表：
{links_text}

严格返回格式：
[{{"title": "标题", "url": "完整URL"}}, ...]"""

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            resp = await client.post(
                f"{GPUSTACK_API_BASE}/chat/completions",
                headers={
                    "Authorization": f"Bearer {GPUSTACK_API_KEY}".encode("utf-8"),
                    "Content-Type": b"application/json",
                },
                json={
                    "model": GPUSTACK_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1,
                    "max_tokens": 4096,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"].strip()
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
            articles = json.loads(content)
            if isinstance(articles, list) and all(isinstance(a, dict) for a in articles):
                logger.info("[阶段2] AI 识别到 %d 篇文章", len(articles))
                if articles:
                    logger.info("[阶段2] 第一篇: 《%s》 %s", articles[0].get("title", "?"), articles[0].get("url", "")[:80])
                return articles
            logger.warning("[阶段2] AI 返回格式异常: %s", type(articles))
            return []
        except Exception as e:
            logger.warning("[阶段2] AI 识别失败: %s", e)
            return []


def fallback_extract_article_links(html: str, base_url: str) -> list[dict]:
    """[阶段3] AI 失败时的降级方案：三阶梯规则提取"""
    soup = BeautifulSoup(html, "lxml")
    articles = []
    seen = set()

    # 3a: URL 白名单正则匹配
    for a in soup.select("a[href]"):
        text = a.get_text(strip=True)
        href = a.get("href", "").strip()
        if not text or not href or len(text) < 3:
            continue
        if href.startswith("#") or href.startswith("javascript:") or href.startswith("mailto:"):
            continue
        full_url = urljoin(base_url, href)
        if full_url in seen:
            continue
        if _is_attachment_url(full_url):
            continue
        if any(pat.search(full_url) for pat in ARTICLE_URL_PATTERNS):
            seen.add(full_url)
            articles.append({"title": text, "url": full_url})

    if articles:
        logger.info("[阶段3a] URL 白名单匹配到 %d 篇文章", len(articles))
        return articles

    # 3b: <li> a 选择器
    for a in soup.select("li a[href]"):
        text = a.get_text(strip=True)
        href = a.get("href", "").strip()
        if not text or not href or len(text) < 3:
            continue
        if href.startswith("#") or href.startswith("javascript:"):
            continue
        if re.match(r"^更多\s*>*$", text):
            continue
        if text in ("首页", "上一页", "下一页", "尾页", ">", ">>", "<", "<<"):
            continue
        if re.match(r"^\d+$", text):
            continue
        full_url = urljoin(base_url, href)
        if full_url in seen:
            continue
        if _is_attachment_url(full_url):
            continue
        seen.add(full_url)
        articles.append({"title": text, "url": full_url})

    if articles:
        logger.info("[阶段3b] li a 选择器匹配到 %d 篇文章", len(articles))
        return articles

    # 3c: 全页面 a 标签 + 启发式过滤
    for a in soup.select("a[href]"):
        text = a.get_text(strip=True)
        href = a.get("href", "").strip()
        if not text or not href or len(text) < 4:
            continue
        if href.startswith("#") or href.startswith("javascript:"):
            continue
        if text in ("首页", "上一页", "下一页", "尾页", ">", ">>", "<", "<<", "更多", "更多>>"):
            continue
        if re.match(r"^\d+$", text):
            continue
        full_url = urljoin(base_url, href)
        if full_url in seen:
            continue
        if _is_attachment_url(full_url):
            continue
        # 启发式：文章 URL 通常比栏目首页更深、更长
        if full_url == base_url or full_url.rstrip("/") == base_url.rstrip("/"):
            continue
        seen.add(full_url)
        articles.append({"title": text, "url": full_url})

    logger.info("[阶段3c] 全页面启发式匹配到 %d 篇文章", len(articles))
    return articles


async def extract_article_links(html: str, base_url: str) -> list[dict]:
    """获取文章链接：AI 优先 + 降级 + 智能合并"""
    ai_result = await ai_extract_article_links(html, base_url)

    if not ai_result or len(ai_result) < 3:
        # AI 失败或返回太少 → 启用降级
        fallback_result = fallback_extract_article_links(html, base_url)

        if not ai_result:
            logger.info("AI 未识别到文章，全部使用降级方案")
            return fallback_result

        # AI 返回 < 3 篇 → 合并两者（取并集，互补）
        fallback_urls = {a["url"] for a in fallback_result}
        merged = list(ai_result)
        for fb in fallback_result:
            if fb["url"] not in {m["url"] for m in merged}:
                merged.append(fb)

        logger.info(
            "AI(%d篇) + 降级(%d篇) 合并 = %d 篇",
            len(ai_result), len(fallback_result), len(merged),
        )
        return merged

    return ai_result


# ==================== 批量爬取 ====================

async def batch_scrape_articles(client: httpx.AsyncClient, urls: list[str]) -> list[dict]:
    """批量爬取文章内容"""
    if not urls:
        return []

    options = {
        "spider_options": {"max_depth": 0, "page_limit": len(urls)},
        "page_options": {
            "include_html": False,
            "only_main_content": True,
            "wait_time": 1000,
            "timeout": 30000,
        },
    }
    resp = await client.post(
        f"{WATERCRAWL_BASE_URL}/crawl-requests/batch/",
        json={"urls": urls, "options": options},
        headers=_wc_headers(),
    )
    resp.raise_for_status()
    uuid = resp.json()["uuid"]

    data = await _wait_crawl(client, uuid, poll_interval=5.0, max_wait=600)
    if data.get("status") != "finished":
        return []

    results = await _get_results(client, uuid)
    enriched = []
    for r in results:
        content = await _get_result_content(client, r)
        enriched.append({
            "uuid": r.get("uuid"),
            "url": r.get("url"),
            "result": content.get("result", content),
        })
    return enriched


# ==================== AI 内容清洗 ====================

def _extract_raw_text(article_data: dict) -> str:
    """从爬取结果中提取可读文本（优先 markdown，退回到 html）"""
    result = article_data.get("result", {})
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        md = result.get("markdown", "")
        if md:
            return md
        html = result.get("html", "")
        if html:
            return BeautifulSoup(html, "lxml").get_text(" ", strip=True)
    return ""


async def ai_clean_all_contents(articles_data: list[dict]) -> list[dict]:
    """使用 Qwen3 分批清洗所有文章内容，提取结构化信息

    分批（每批 10 篇）以避免超出 max_tokens 输出限制，
    兼容任意篇数（0~30+）。
    """
    if not articles_data:
        return []

    BATCH_SIZE = 10
    all_cleaned = []

    for start in range(0, len(articles_data), BATCH_SIZE):
        batch = articles_data[start:start + BATCH_SIZE]
        logger.info(
            "[清洗] 批次 %d: 第 %d-%d 篇",
            start // BATCH_SIZE + 1,
            start + 1,
            start + len(batch),
        )

        texts = []
        for i, ad in enumerate(batch):
            raw = _extract_raw_text(ad)
            if not raw or len(raw) < 50:
                texts.append(f"[{start + i}] (内容不足)")
            else:
                texts.append(f"[{start + i}]\n{raw[:5000]}")

        batch_text = "\n\n=====\n\n".join(texts)

        prompt = f"""以下是从政府网站抓取到的 {len(batch)} 篇文章的网页全文，每篇以 "=====" 分隔。

请对每篇文章：先生成一段 200 字以内的摘要，概括文章核心内容。

按以下格式回复（每篇文章之间用 "===" 分隔）：

TTL: 文章标题
DTM: 发文日期（如有，格式 YYYY-MM-DD）
SRC: 发布单位/来源（如有，无则留空）
ABS:
摘要内容（200字以内）

===
TTL: 下一篇文章标题
...

注意：
- TTL/ABS 必须有，DTM/SRC 没有则留空即可
- 不要加任何其他说明文字

内容如下：
{batch_text}"""

        async with httpx.AsyncClient(timeout=180.0) as client:
            try:
                resp = await client.post(
                    f"{GPUSTACK_API_BASE}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {GPUSTACK_API_KEY}".encode("utf-8"),
                        "Content-Type": b"application/json",
                    },
                    json={
                        "model": GPUSTACK_MODEL,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.1,
                        "max_tokens": 5120,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                raw_response = data["choices"][0]["message"]["content"].strip()
                logger.info(
                    "[清洗] 批次 %d AI 响应 (前200字符): %s",
                    start // BATCH_SIZE + 1,
                    raw_response[:200].replace("\n", "\\n"),
                )

                # 按 === 分割每篇文章
                blocks = [b.strip() for b in raw_response.split("===") if b.strip()]
                for block in blocks:
                    lines = block.strip().split("\n")
                    item = {"title": "", "publish_date": "", "source": "", "body": ""}
                    current_field = None
                    for line in lines:
                        if line.startswith("TTL:"):
                            item["title"] = line[4:].strip()
                            current_field = None
                        elif line.startswith("DTM:"):
                            item["publish_date"] = line[4:].strip()
                            current_field = None
                        elif line.startswith("SRC:"):
                            item["source"] = line[4:].strip()
                            current_field = None
                        elif line.startswith("ABS:"):
                            current_field = "body"
                        elif current_field == "body":
                            item["body"] += line + "\n"
                    item["body"] = item["body"].strip()
                    if item["title"]:
                        all_cleaned.append(item)

            except Exception as e:
                logger.warning("[清洗] 批次 %d 清洗失败: %s", start // BATCH_SIZE + 1, e)

    logger.info("[清洗] AI 分批清洗完成: %d 篇", len(all_cleaned))
    return all_cleaned


# ==================== AI 摘要生成 ====================

async def ai_generate_summary(titles: list[str]) -> str:
    """使用 Qwen3-32B 根据文章标题列表生成摘要"""
    if not titles:
        return ""

    titles_text = "\n".join(f"{i+1}. {t}" for i, t in enumerate(titles))

    prompt = f"""以下是某政府网站一个列表页的所有文章标题。请根据这些标题生成一段约200字的摘要，概括这些文章的主题和关注重点。

文章标题列表（共{len(titles)}篇）：
{titles_text}

要求：
- 用一段连贯的中文文字概括
- 突出主题分布和核心关注点
- 约200字即可
- 只返回摘要文字，不要任何额外说明"""

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            resp = await client.post(
                f"{GPUSTACK_API_BASE}/chat/completions",
                headers={
                    "Authorization": f"Bearer {GPUSTACK_API_KEY}".encode("utf-8"),
                    "Content-Type": b"application/json",
                },
                json={
                    "model": GPUSTACK_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                    "max_tokens": 500,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            summary = data["choices"][0]["message"]["content"].strip()
            logger.info("[摘要] 生成完成: %s...", summary[:80])
            return summary
        except Exception as e:
            logger.warning("[摘要] 生成失败: %s", e)
            return ""


# ==================== Redis 任务队列 ====================

def _task_key(task_id: str) -> str:
    return f"{TASK_PREFIX}{task_id}"


class TaskManager:
    """基于 Redis 的任务队列管理器"""

    def __init__(self):
        self.redis: Optional[aioredis.Redis] = None

    async def init(self):
        self.redis = aioredis.from_url(REDIS_URL, decode_responses=True)
        await self.redis.ping()
        logger.info("Redis 连接成功: %s", REDIS_URL)

    async def close(self):
        if self.redis:
            await self.redis.close()

    async def create_task(self, url: str) -> str:
        """创建爬取任务，入队，返回 task_id"""
        task_id = str(uuid.uuid4())[:8]
        now = datetime.now(timezone.utc).isoformat()
        await self.redis.hset(
            _task_key(task_id),
            mapping={
                "url": url,
                "status": "pending",
                "progress": "任务已加入队列",
                "created_at": now,
                "links": "[]",
                "results": "[]",
                "summary": "",
            },
        )
        await self.redis.rpush(QUEUE_KEY, task_id)
        # 设置过期时间，防止内存泄漏
        await self.redis.expire(_task_key(task_id), TASK_TTL)
        logger.info("任务已入队: task_id=%s, url=%s", task_id, url)
        return task_id

    async def update_task(self, task_id: str, **kwargs):
        """更新任务字段"""
        await self.redis.hset(_task_key(task_id), mapping=kwargs)

    async def get_task(self, task_id: str) -> Optional[dict]:
        """获取任务完整信息"""
        data = await self.redis.hgetall(_task_key(task_id))
        if not data:
            return None
        return data

    async def get_links(self, task_id: str) -> list:
        raw = await self.redis.hget(_task_key(task_id), "links")
        return json.loads(raw) if raw else []

    async def get_results(self, task_id: str) -> list:
        raw = await self.redis.hget(_task_key(task_id), "results")
        return json.loads(raw) if raw else []

    async def get_summary(self, task_id: str) -> str:
        return await self.redis.hget(_task_key(task_id), "summary") or ""

    async def store_results(self, task_id: str, links: list, results: list, summary: str):
        """存储爬取结果"""
        await self.redis.hset(
            _task_key(task_id),
            mapping={
                "links": json.dumps(links, ensure_ascii=False),
                "results": json.dumps(results, ensure_ascii=False),
                "summary": summary,
            },
        )


# ==================== 工作进程 ====================


async def execute_crawl(task_id: str, url: str):
    """执行单个爬取任务"""
    await task_manager.update_task(task_id, status="running", progress="Layer 1: 正在爬取列表页...")

    all_links = []
    merged = []
    summary = ""

    async with httpx.AsyncClient(timeout=600.0) as client:
        try:
            # ---- Layer 1 ----
            await task_manager.update_task(task_id, progress="Layer 1: 正在爬取列表页...")
            logger.info("=== Layer 1: 爬取列表页 === task=%s", task_id)
            page = await _scrape_page(client, url)
            if page["status"] != "finished" or not page["html"]:
                raise RuntimeError(f"列表页爬取失败: {page['status']}")
            logger.info("列表页爬取成功: html_len=%d", len(page["html"]))

            # ---- iframe ----
            iframe_soup = BeautifulSoup(page["html"], "lxml")
            iframes = iframe_soup.find_all("iframe", src=True) or iframe_soup.find_all("frame", src=True)
            if iframes:
                logger.info("检测到 %d 个 iframe/frame", len(iframes))
                for idx, ifr in enumerate(iframes):
                    iframe_url = urljoin(url, ifr["src"])
                    await task_manager.update_task(task_id, progress=f"Layer 1: 正在爬取 iframe({idx+1}/{len(iframes)})...")
                    iframe_page = await _scrape_page(client, iframe_url)
                    if iframe_page["html"]:
                        page["html"] += f"\n<!-- iframe {idx} content -->\n" + iframe_page["html"]
                logger.info("iframe 全部爬取完成, 合并后 html_len=%d", len(page["html"]))

            # ---- AI 识别 ----
            await task_manager.update_task(task_id, progress="Layer 1: AI 正在识别文章链接...")
            articles_info = await extract_article_links(page["html"], url)
            if not articles_info:
                raise RuntimeError("未能识别到任何文章链接")

            # 统一截断至 30 篇（links + results 一致）
            if len(articles_info) > 30:
                logger.info("文章数 %d 超过上限 30, 截取前30篇", len(articles_info))
                articles_info = articles_info[:30]

            all_links = [{"title": a["title"], "url": a["url"]} for a in articles_info]
            logger.info("最终 %d 篇文章", len(all_links))

            # ---- Layer 2 ----
            article_urls = [a["url"] for a in articles_info]

            await task_manager.update_task(
                task_id,
                progress=f"Layer 2: 正在批量爬取 {len(article_urls)} 篇文章...",
            )
            batch_results = await batch_scrape_articles(client, article_urls)

            # 按 URL 构建结果索引，避免因部分 URL 爬取失败导致索引错位
            result_by_url = {r["url"]: r for r in batch_results}
            missing_urls = [
                u for u in article_urls if u not in result_by_url
            ]
            if missing_urls:
                logger.warning(
                    "WaterCrawl 批量爬取丢失 %d/%d 篇，缺失 URL: %s",
                    len(missing_urls), len(article_urls),
                    [u[:80] for u in missing_urls],
                )

            # ---- AI 清洗 ----
            await task_manager.update_task(task_id, progress="正在 AI 清洗文章内容...")
            # 只清洗成功爬取到的文章
            matched_results = [result_by_url[u] for u in article_urls if u in result_by_url]
            cleaned = await ai_clean_all_contents(matched_results)
            # 用爬取结果的 url 匹配 cleaned（按索引同序）
            cleaned_by_url = {}
            for i, c in enumerate(cleaned):
                if i < len(matched_results):
                    cleaned_by_url[matched_results[i]["url"]] = c

            for info in articles_info:
                url = info["url"]
                item = {"title": info["title"], "url": url, "content": None}
                if url in result_by_url:
                    item["content"] = result_by_url[url]
                if url in cleaned_by_url:
                    item["cleaned"] = cleaned_by_url[url]
                merged.append(item)

            # ---- AI 摘要 ----
            if merged:
                await task_manager.update_task(task_id, progress="正在生成 AI 摘要...")
                titles = [m.get("cleaned", {}).get("title", m["title"]) for m in merged]
                summary = await ai_generate_summary(titles)

            # ---- 存储结果 ----
            await task_manager.store_results(task_id, all_links, merged, summary)
            await task_manager.update_task(
                task_id,
                status="finished",
                progress=f"全部完成: {len(merged)} 篇文章 + AI 摘要",
            )
            logger.info("任务完成: task_id=%s, 文章=%d", task_id, len(merged))

        except Exception as e:
            logger.exception("爬取过程发生异常: task_id=%s", task_id)
            await task_manager.update_task(task_id, status="failed", progress=f"爬取出错: {e}")


async def worker(worker_id: int):
    """工作进程：从队列取任务并执行"""
    logger.info("Worker[%d] 已启动", worker_id)
    while True:
        try:
            _, task_id = await task_manager.redis.brpop(QUEUE_KEY, timeout=0)
            logger.info("Worker[%d] 取到任务: %s", worker_id, task_id)
            task = await task_manager.get_task(task_id)
            if not task:
                logger.warning("Worker[%d] 任务不存在: %s", worker_id, task_id)
                continue
            url = task.get("url", "")
            await execute_crawl(task_id, url)
        except Exception as e:
            logger.error("Worker[%d] 异常: %s", worker_id, e)
            await asyncio.sleep(5)


async def start_workers():
    """启动所有工作进程（独立运行，互不影响）"""
    await task_manager.init()
    worker_tasks = []
    for i in range(WORKER_COUNT):
        t = asyncio.create_task(worker(i), name=f"worker-{i}")
        worker_tasks.append(t)
    logger.info("已启动 %d 个工作进程", WORKER_COUNT)
    # 用 wait：检测到任何 Worker 退出时，取消其他 Worker 并整体重启
    done, pending = await asyncio.wait(worker_tasks, return_when=asyncio.FIRST_EXCEPTION)
    for t in done:
        exc = t.exception()
        if exc:
            logger.critical("Worker 异常退出: %s", exc)
    # 取消仍在运行的 Worker，避免重复进程
    for t in pending:
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
    logger.critical("所有 Worker 已停止，5 秒后重启...")
    await asyncio.sleep(5)
    asyncio.create_task(start_workers(), name="workers-restart")


task_manager = TaskManager()
