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
from typing import Optional
from urllib.parse import urljoin

import httpx
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

# 文章链接筛选黑名单（这些 URL 模式通常不是文章）
EXCLUDE_URL_PATTERNS = [
    re.compile(r"javascript", re.I),
    re.compile(r"^#$"),
    re.compile(r"/index\.htm"),
    re.compile(r"/index_\d+\.htm"),
    re.compile(r"\.docx?$", re.I),
    re.compile(r"\.xlsx?$", re.I),
    re.compile(r"\.pdf$", re.I),
    re.compile(r"\.zip$", re.I),
]

# 文章链接筛选白名单（优先用这些模式匹配）
ARTICLE_URL_PATTERNS = [
    re.compile(r"/art/", re.I),                    # nhsa 模式
    re.compile(r"/\d{6}/t\d{8}_\d+\.htm"),         # mof 模式: /202607/t20260708_3993182.htm
    re.compile(r"/\d{4}-\d{2}/\d{2}/c_\d+\.htm"),  # cac 模式: /2026-07/06/c_1785086223921593.htm
    re.compile(r"/content/\d+"),                    # 通用 content 模式
    re.compile(r"/\d{4}-\d{2}/\d{2}/content_\d+"),  # 通用 content 模式
    re.compile(r"/info/\d+"),                       # 通用 info 模式
]


def _is_article_url(url: str) -> bool:
    """判断一个 URL 是否可能是文章链接"""
    for pat in EXCLUDE_URL_PATTERNS:
        if pat.search(url):
            return False
    return True


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
    """从 HTML 中提取所有有意义的链接，供 AI 分析"""
    soup = BeautifulSoup(html, "lxml")
    links = []
    seen = set()

    for tag in soup.select("a[href]"):
        text = tag.get_text(strip=True)
        href = tag.get("href", "").strip()
        if not text or not href or len(text) < 3:
            continue
        if href.startswith("#") or href.startswith("javascript"):
            continue
        full_url = urljoin(base_url, href)
        if full_url in seen:
            continue
        if not _is_article_url(full_url):
            continue
        seen.add(full_url)
        parent_text = ""
        parent = tag.parent
        if parent:
            parent_text = parent.get_text(" ", strip=True)[:100]
        links.append({"text": text, "url": full_url, "context": parent_text})
    return links


async def ai_extract_article_links(html: str, base_url: str) -> list[dict]:
    """使用 Qwen3-32B 从列表页 HTML 中智能提取文章链接"""
    all_links = _extract_links_from_html(html, base_url)
    if not all_links:
        logger.warning("HTML 中未提取到任何链接")
        return []

    # 截取前 300 个链接
    links_text = json.dumps(all_links[:300], ensure_ascii=False, indent=2)

    prompt = f"""你是一个网页分析助手。以下是从一个政府网站的列表页提取到的所有链接。

请从中筛选出真正的「文章」链接（即每一条法规、政策、通知、公告等正文页面的链接），排除以下类型：
- 导航栏链接（如"首页""上一页""下一页""尾页"等）
- 分页链接（如页码 1/2/3）
- 非文章页（如栏目首页、PDF附件、附件下载链接等）
- 面包屑链接

要求：
- 每条文章返回 title（标题文字）和 url（完整链接）
- title 为空或明显不是文章标题的不要
- 只返回 JSON 数组，不要任何其他文字

链接列表：
{links_text}

严格按以下 JSON 格式返回（不要 markdown 代码块标记）：
[{{"title": "文章标题", "url": "https://..."}}, ...]"""

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
                    "max_tokens": 4000,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"].strip()
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
            articles = json.loads(content)
            if isinstance(articles, list) and all(isinstance(a, dict) for a in articles):
                logger.info("AI 识别到 %d 篇文章", len(articles))
                return articles
            return []
        except Exception as e:
            logger.warning("AI 识别文章链接失败: %s", e)
            return []


def fallback_extract_article_links(html: str, base_url: str) -> list[dict]:
    """AI 失败时的降级方案：规则提取文章链接"""
    soup = BeautifulSoup(html, "lxml")
    articles = []
    seen = set()

    # 方法1: 用 URL 白名单模式匹配
    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()
        text = a.get_text(strip=True)
        if not text or not href or len(text) < 4:
            continue
        if href.startswith("#") or href.startswith("javascript"):
            continue
        full_url = urljoin(base_url, href)
        if full_url in seen:
            continue
        if not _is_article_url(full_url):
            continue
        # 检查是否匹配已知文章 URL 模式
        matched = any(pat.search(full_url) for pat in ARTICLE_URL_PATTERNS)
        if matched:
            seen.add(full_url)
            articles.append({"title": text, "url": full_url})

    # 方法2: 回退到 li a 选择器
    if not articles:
        for a in soup.select("li a[href]"):
            text = a.get_text(strip=True)
            href = a.get("href", "").strip()
            if not text or not href or len(text) < 4:
                continue
            if href.startswith("#") or href.startswith("javascript"):
                continue
            if re.match(r"^更多\s*>*$", text):
                continue
            if text in ("首页", "上一页", "下一页", "尾页", ">", ">>", "<", "<<"):
                continue
            full_url = urljoin(base_url, href)
            if full_url in seen:
                continue
            if not _is_article_url(full_url):
                continue
            seen.add(full_url)
            articles.append({"title": text, "url": full_url})

    return articles


async def extract_article_links(html: str, base_url: str) -> list[dict]:
    """获取文章链接：先用 AI，失败则降级"""
    result = await ai_extract_article_links(html, base_url)
    if result:
        return result
    logger.info("AI 未识别到文章，使用降级方案")
    return fallback_extract_article_links(html, base_url)


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


# ==================== 主服务 ====================

class GovCrawlerService:
    """政府网站列表页文章爬取服务"""

    def __init__(self):
        self._status = "idle"
        self._progress = ""
        self._target_url: str = ""
        self._results: list = []

    @property
    def status(self) -> str:
        return self._status

    @property
    def progress(self) -> str:
        return self._progress

    def get_results(self) -> list:
        return self._results

    async def crawl_url(self, url: str):
        """对指定 URL 执行完整两层爬取"""
        self._status = "running"
        self._progress = ""
        self._results = []
        self._target_url = url

        async with httpx.AsyncClient(timeout=600.0) as client:
            try:
                # ---- Layer 1: 爬取列表页 + AI 识别文章链接 ----
                self._progress = "Layer 1: 正在爬取列表页..."
                logger.info("=== Layer 1: 爬取列表页 ===")
                page = await _scrape_page(client, url)
                if page["status"] != "finished" or not page["html"]:
                    self._status = "failed"
                    self._progress = f"列表页爬取失败 (status={page['status']}, html_len={len(page.get('html',''))})"
                    logger.error("列表页爬取失败: status=%s, html_len=%d", page["status"], len(page.get("html", "")))
                    return
                logger.info("列表页爬取成功: html_len=%d", len(page["html"]))

                self._progress = "Layer 1: AI 正在识别文章链接..."
                articles_info = await extract_article_links(page["html"], url)
                if not articles_info:
                    self._status = "failed"
                    self._progress = "未能识别到任何文章链接，请检查目标页面结构"
                    return
                self._progress = f"Layer 1 完成: 识别到 {len(articles_info)} 篇文章"

                # ---- Layer 2: 批量爬取文章 ----
                article_urls = [a["url"] for a in articles_info]
                self._progress = f"Layer 2: 正在批量爬取 {len(article_urls)} 篇文章..."
                batch_results = await batch_scrape_articles(client, article_urls)

                merged = []
                for i, info in enumerate(articles_info):
                    item = {"title": info["title"], "url": info["url"], "content": None}
                    if i < len(batch_results):
                        item["content"] = batch_results[i]
                    merged.append(item)

                self._results = merged
                self._status = "finished"
                self._progress = f"爬取完成: 共 {len(merged)} 篇文章"

            except Exception as e:
                self._status = "failed"
                self._progress = f"爬取出错: {e}"
                logger.exception("爬取过程发生异常")


crawler_service = GovCrawlerService()
