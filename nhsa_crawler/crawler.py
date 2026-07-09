"""
国家医保局网站爬取服务 - 三层爬取架构
Layer 1: 首页 → AI 识别栏目链接
Layer 2: 栏目列表页 → 解析文章链接
Layer 3: 文章详情页 → 批量爬取内容，按栏目分类存储
"""
import asyncio
import json
import logging
import os
import re
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger("nhsa_crawler")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(handler)

# ---- 环境变量 ----
WATERCRAWL_BASE_URL = os.getenv(
    "WATERCRAWL_BASE_URL", "http://10.60.151.130:7109/api/v1/core"
)
WATERCRAWL_API_KEY = os.getenv("WATERCRAWL_API_KEY", "")

GPUSTACK_API_BASE = os.getenv(
    "GPUSTACK_API_BASE", "https://gpustack.stock.hnchasing.com/v1"
)
GPUSTACK_API_KEY = os.getenv("GPUSTACK_API_KEY", "")
GPUSTACK_MODEL = os.getenv("GPUSTACK_MODEL", "qwen3-32b")

NHSA_HOMEPAGE = "https://www.nhsa.gov.cn/"

# 栏目名称映射
SECTION_LABELS = {
    "yibao_zhengce": "医保政策",
    "dongtai": "动态",
    "tongji_shuju": "统计数据",
}

# 关键词降级备用（AI 失败时使用）
FALLBACK_KEYWORDS = {
    "yibao_zhengce": ["政策", "政务信息", "政策文件"],
    "dongtai": ["医保动态", "新闻动态", "工作动态"],
    "tongji_shuju": ["统计数据", "统计信息", "数智库"],
}


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
    url = result.get("result", "")
    if url and isinstance(url, str) and url.startswith("http"):
        try:
            r = await client.get(url)
            r.raise_for_status()
            return r.json()
        except Exception:
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


# ==================== GPUStack / Qwen3-32B AI ====================

def _extract_links_from_html(html: str) -> list[dict]:
    """从 HTML 中提取所有有意义的链接（导航区 + 内容区）"""
    soup = BeautifulSoup(html, "lxml")
    links = []
    seen = set()

    # 优先从导航区域提取
    for tag in soup.select("a[href]"):
        text = tag.get_text(strip=True)
        href = tag.get("href", "").strip()
        if not text or not href or len(text) < 2:
            continue
        if href.startswith("#") or href.startswith("javascript"):
            continue
        full_url = urljoin(NHSA_HOMEPAGE, href)
        if full_url in seen:
            continue
        seen.add(full_url)
        # 提取周围上下文（父节点文字）
        parent_text = ""
        parent = tag.parent
        if parent:
            parent_text = parent.get_text(" ", strip=True)[:100]
        links.append({"text": text, "url": full_url, "context": parent_text})
    return links


async def ai_identify_sections(html: str) -> dict[str, str]:
    """使用 Qwen3-32B 从首页链接中识别三个目标栏目的 URL"""
    all_links = _extract_links_from_html(html)
    if not all_links:
        return {}

    # 截取前 200 个链接避免 prompt 过长
    links_text = json.dumps(all_links[:200], ensure_ascii=False, indent=2)

    prompt = f"""你是一个网页分析助手。以下是从国家医保局官网(www.nhsa.gov.cn)首页提取到的导航链接列表。
请从中找出以下三个栏目的链接：
1. "医保政策" — 政策文件、政务信息相关的栏目
2. "动态" — 医保动态、新闻动态、工作动态相关的栏目
3. "统计数据" — 统计信息、数智库相关的栏目

要求：
- 如果找到完全匹配的，直接输出对应链接
- 如果找不到完全匹配的，根据链接文字和上下文语义选择最接近的一个
- 如果某个栏目确实找不到，对应值设为空字符串 ""
- 只返回 JSON，不要任何其他文字

链接列表：
{links_text}

请严格按以下 JSON 格式返回（不要 markdown 代码块标记）：
{{"yibao_zhengce": "url1", "dongtai": "url2", "tongji_shuju": "url3"}}"""

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
                    "max_tokens": 500,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"].strip()
            # 清理可能的 markdown 代码块标记
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
            result = json.loads(content)
            # 验证返回格式
            validated = {}
            for key in ("yibao_zhengce", "dongtai", "tongji_shuju"):
                validated[key] = result.get(key, "")
            return validated
        except Exception as e:
            print(f"[AI识别失败] {e}, 降级到关键词匹配")
            return {}


def fallback_identify_sections(html: str) -> dict[str, str]:
    """AI 失败时的降级方案：用关键词匹配"""
    soup = BeautifulSoup(html, "lxml")
    links = {}
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        href = a["href"]
        if not text or not href:
            continue
        full_url = urljoin(NHSA_HOMEPAGE, href)
        for key, keywords in FALLBACK_KEYWORDS.items():
            if key in links:
                continue
            if any(kw in text for kw in keywords):
                links[key] = full_url
    return links


async def get_section_links(html: str) -> dict[str, str]:
    """获取栏目链接：先用 AI，失败则降级"""
    result = await ai_identify_sections(html)
    if result and result.get("yibao_zhengce") and result.get("dongtai"):
        return result
    fallback_result = fallback_identify_sections(html)
    for key in ("yibao_zhengce", "dongtai", "tongji_shuju"):
        if key not in fallback_result or not fallback_result[key]:
            # 将 AI 结果合并过来
            fallback_result[key] = result.get(key, "") if result else ""
    return fallback_result


# ==================== 列表页解析 ====================

def extract_article_links(html: str, base_url: str) -> list[dict]:
    """从栏目列表页 HTML 中提取文章链接和标题"""
    soup = BeautifulSoup(html, "lxml")
    articles = []
    seen = set()

    # 常见列表选择器
    selectors = [
        "ul.news_list li a",
        "ul.list li a",
        "div.news_list li a",
        "div.list li a",
        "div.content ul li a",
        "ul li a",
    ]
    links = []
    for sel in selectors:
        links = soup.select(sel)
        if links:
            break
    if not links:
        # 宽松匹配：找所有在列表场景下的 a 标签
        links = soup.select("li a[href]")
    if not links:
        links = soup.select("a[href]")

    for a in links:
        text = a.get_text(strip=True)
        href = a.get("href", "").strip()
        if not text or not href:
            continue
        if len(text) < 4:
            continue
        if href.startswith("#") or href.startswith("javascript"):
            continue
        # 过滤掉"更多>>"这种链接
        if re.match(r"^更多\s*>*$", text):
            continue
        full_url = urljoin(base_url, href)
        if full_url in seen:
            continue
        seen.add(full_url)
        articles.append({"title": text, "url": full_url})
    return articles


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

class NhsCrawlerService:
    """医保局网站三层爬取服务"""

    def __init__(self):
        self._status = "idle"
        self._progress = ""
        self._section_links: dict[str, str] = {}
        self._results: dict[str, list] = {}

    @property
    def status(self) -> str:
        return self._status

    @property
    def progress(self) -> str:
        return self._progress

    def get_results(self, section: str = None) -> dict | list:
        if section:
            return self._results.get(section, [])
        return self._results

    async def crawl_all(self):
        """完整三层爬取流程"""
        self._status = "running"
        self._results = {}
        self._section_links = {}

        async with httpx.AsyncClient(timeout=600.0) as client:
            try:
                # ---- Layer 1: 爬取首页 + AI 识别栏目 ----
                self._progress = "Layer 1: 正在爬取首页..."
                logger.info("=== Layer 1: 爬取首页 ===")
                page = await _scrape_page(client, NHSA_HOMEPAGE)
                if page["status"] != "finished" or not page["html"]:
                    self._status = "failed"
                    self._progress = f"首页爬取失败 (status={page['status']}, html_len={len(page.get('html',''))})"
                    logger.error("首页爬取失败: status=%s, html_len=%d", page["status"], len(page.get("html", "")))
                    return
                logger.info("首页爬取成功: html_len=%d", len(page["html"]))

                self._progress = "Layer 1: AI 正在识别栏目链接..."
                self._section_links = await get_section_links(page["html"])
                found_count = sum(1 for v in self._section_links.values() if v)
                if found_count == 0:
                    self._status = "failed"
                    self._progress = "未能识别到任何栏目链接，请检查首页结构是否变化"
                    return
                self._progress = f"Layer 1 完成: 识别到 {found_count} 个栏目链接"

                # ---- Layer 2: 爬取各栏目列表页 + 解析文章链接 ----
                all_articles = {}

                for section_key, section_url in self._section_links.items():
                    if not section_url:
                        self._results[section_key] = []
                        continue

                    label = SECTION_LABELS.get(section_key, section_key)
                    self._progress = f"Layer 2: 正在爬取【{label}】列表页..."
                    list_page = await _scrape_page(client, section_url)
                    if list_page["status"] != "finished" or not list_page["html"]:
                        self._progress = f"【{label}】列表页爬取失败，跳过"
                        self._results[section_key] = []
                        continue

                    articles = extract_article_links(list_page["html"], section_url)
                    self._progress = f"【{label}】列表页找到 {len(articles)} 篇文章"
                    all_articles[section_key] = {
                        "section_label": label,
                        "section_url": section_url,
                        "articles_info": articles,
                    }

                # ---- Layer 3: 批量爬取文章 + 按栏目归类 ----
                for section_key, data in all_articles.items():
                    label = data["section_label"]
                    article_urls = [a["url"] for a in data["articles_info"]]

                    if not article_urls:
                        self._results[section_key] = []
                        continue

                    self._progress = f"Layer 3: 正在批量爬取【{label}】{len(article_urls)} 篇文章..."
                    batch_results = await batch_scrape_articles(client, article_urls)

                    merged = []
                    for i, info in enumerate(data["articles_info"]):
                        item = {"title": info["title"], "url": info["url"], "content": None}
                        if i < len(batch_results):
                            item["content"] = batch_results[i]
                        merged.append(item)

                    self._results[section_key] = merged
                    self._progress = f"【{label}】爬取完成: {len(merged)} 篇文章"

                self._status = "finished"
                self._progress = "爬取全部完成！"

            except Exception as e:
                self._status = "failed"
                self._progress = f"爬取出错: {e}"
                logger.exception("爬取过程发生异常")


crawler_service = NhsCrawlerService()
