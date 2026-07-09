import asyncio
import os
import re
from typing import Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

WATERCRAWL_BASE_URL = os.getenv(
    "WATERCRAWL_BASE_URL", "http://10.60.151.130:80/api/v1/core"
)
WATERCRAWL_API_KEY = os.getenv("WATERCRAWL_API_KEY", "")

NHSA_HOMEPAGE = "https://www.nhsa.gov.cn/"

SECTION_KEYWORDS = {
    "yibao_zhengce": ["医保政策"],
    "dongtai": ["动态", "新闻动态", "工作动态"],
    "tongji_shuju": ["统计数据", "统计信息", "统计"],
}


def _headers():
    return {
        "X-API-Key": WATERCRAWL_API_KEY,
        "Content-Type": "application/json",
    }


async def _create_crawl(client: httpx.AsyncClient, url: str, page_options: dict = None) -> str:
    """创建爬取任务，返回 uuid"""
    options = {
        "spider_options": {"max_depth": 0, "page_limit": 1},
        "page_options": page_options
        or {
            "include_html": True,
            "only_main_content": False,
            "wait_time": 2000,
            "timeout": 30000,
        },
    }
    resp = await client.post(
        f"{WATERCRAWL_BASE_URL}/crawl-requests/",
        json={"url": url, "options": options},
        headers=_headers(),
    )
    resp.raise_for_status()
    return resp.json()["uuid"]


async def _wait_crawl(client: httpx.AsyncClient, uuid: str, poll_interval: float = 2.0, max_wait: float = 120) -> dict:
    """轮询等待爬取完成"""
    elapsed = 0.0
    while elapsed < max_wait:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
        resp = await client.get(
            f"{WATERCRAWL_BASE_URL}/crawl-requests/{uuid}/",
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        if data["status"] in ("finished", "failed", "canceled"):
            return data
    raise TimeoutError(f"Crawl {uuid} timed out after {max_wait}s")


async def _get_results(client: httpx.AsyncClient, uuid: str) -> list[dict]:
    """获取爬取结果列表"""
    resp = await client.get(
        f"{WATERCRAWL_BASE_URL}/crawl-requests/{uuid}/results/",
        headers=_headers(),
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("results", [])


async def _get_result_content(client: httpx.AsyncClient, result: dict) -> dict:
    """如果 result 是文件链接，则下载内容"""
    result_url = result.get("result", "")
    if result_url and result_url.startswith("http"):
        try:
            resp = await client.get(result_url)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return {}
    return result if isinstance(result, dict) else {}


async def scrape_url(url: str, page_options: dict = None) -> dict:
    """爬取单个 URL，返回完整的爬取结果（含内容）"""
    async with httpx.AsyncClient(timeout=300.0) as client:
        uuid = await _create_crawl(client, url, page_options)
        crawl_data = await _wait_crawl(client, uuid)
        if crawl_data.get("status") != "finished":
            return {"status": crawl_data.get("status"), "results": []}
        results = await _get_results(client, uuid)
        enriched = []
        for r in results:
            content = await _get_result_content(client, r)
            enriched.append({
                "uuid": r.get("uuid"),
                "url": r.get("url"),
                "result": content.get("result", content),
            })
        return {"status": "finished", "uuid": uuid, "results": enriched}


def extract_section_links(html: str) -> dict[str, str]:
    """从首页 HTML 中提取三个目标栏目的链接"""
    soup = BeautifulSoup(html, "lxml")
    links = {}
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        href = a["href"]
        if not text or not href:
            continue
        full_url = urljoin(NHSA_HOMEPAGE, href)
        for key, keywords in SECTION_KEYWORDS.items():
            if key in links:
                continue
            if any(kw in text for kw in keywords):
                links[key] = full_url
    return links


def extract_article_links(html: str, base_url: str) -> list[dict[str, str]]:
    """从列表页 HTML 中提取所有文章链接及标题"""
    soup = BeautifulSoup(html, "lxml")
    articles = []
    seen = set()

    content_area = (
        soup.find("div", class_=re.compile(r"list|content|main|article", re.I))
        or soup.find("ul", class_=re.compile(r"list|news", re.I))
        or soup
    )

    for a in content_area.find_all("a", href=True):
        text = a.get_text(strip=True)
        href = a["href"]
        if not text or len(text) < 4:
            continue
        full_url = urljoin(base_url, href)
        if full_url in seen:
            continue
        seen.add(full_url)
        articles.append({"title": text, "url": full_url})

    return articles


async def batch_scrape_articles(urls: list[str]) -> dict:
    """批量爬取文章列表"""
    if not urls:
        return {"status": "finished", "results": []}

    async with httpx.AsyncClient(timeout=600.0) as client:
        options = {
            "spider_options": {
                "max_depth": 0,
                "page_limit": len(urls),
            },
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
            headers=_headers(),
        )
        resp.raise_for_status()
        uuid = resp.json()["uuid"]

        crawl_data = await _wait_crawl(client, uuid, poll_interval=5.0, max_wait=600)
        if crawl_data.get("status") != "finished":
            return {"status": crawl_data.get("status"), "uuid": uuid, "results": []}

        results = await _get_results(client, uuid)
        enriched = []
        for r in results:
            content = await _get_result_content(client, r)
            enriched.append({
                "uuid": r.get("uuid"),
                "url": r.get("url"),
                "result": content.get("result", content),
            })
        return {"status": "finished", "uuid": uuid, "results": enriched}


class NhsCrawlerService:
    """医保局网站爬取服务"""

    def __init__(self):
        self._status = "idle"
        self._progress = ""
        self._results: dict[str, list] = {}

    @property
    def status(self) -> str:
        return self._status

    @property
    def progress(self) -> str:
        return self._progress

    def get_results(self, section: str = None):
        if section:
            return self._results.get(section, [])
        return self._results

    async def crawl_all(self):
        """执行完整的爬取流程"""
        self._status = "running"
        self._results = {}
        try:
            self._progress = "正在爬取首页，获取栏目链接..."
            homepage_result = await scrape_url(NHSA_HOMEPAGE)

            homepage_html = ""
            if homepage_result.get("results"):
                first = homepage_result["results"][0]
                result_data = first.get("result", {})
                if isinstance(result_data, dict):
                    homepage_html = result_data.get("html", result_data.get("markdown", ""))
                elif isinstance(result_data, str):
                    homepage_html = result_data

            section_links = extract_section_links(homepage_html)
            if len(section_links) < 3:
                self._progress = f"警告：仅找到 {len(section_links)} 个栏目链接"

            all_articles = {}

            for section_key, section_url in section_links.items():
                section_label = {
                    "yibao_zhengce": "医保政策",
                    "dongtai": "动态",
                    "tongji_shuju": "统计数据",
                }.get(section_key, section_key)

                self._progress = f"正在爬取【{section_label}】列表页..."
                list_result = await scrape_url(section_url)
                list_html = ""
                if list_result.get("results"):
                    first = list_result["results"][0]
                    result_data = first.get("result", {})
                    if isinstance(result_data, dict):
                        list_html = result_data.get("html", result_data.get("markdown", ""))
                    elif isinstance(result_data, str):
                        list_html = result_data

                articles = extract_article_links(list_html, section_url)
                self._progress = f"【{section_label}】列表页找到 {len(articles)} 篇文章"
                all_articles[section_key] = {
                    "section_label": section_label,
                    "section_url": section_url,
                    "articles_info": articles,
                }

            for section_key, data in all_articles.items():
                section_label = data["section_label"]
                article_urls = [a["url"] for a in data["articles_info"]]

                if not article_urls:
                    self._results[section_key] = []
                    continue

                self._progress = f"正在批量爬取【{section_label}】{len(article_urls)} 篇文章..."
                batch_result = await batch_scrape_articles(article_urls)

                merged = []
                for i, article_info in enumerate(data["articles_info"]):
                    article_data = {"title": article_info["title"], "url": article_info["url"], "content": None}
                    if i < len(batch_result.get("results", [])):
                        article_data["content"] = batch_result["results"][i]
                    merged.append(article_data)

                self._results[section_key] = merged

            self._status = "finished"
            self._progress = "爬取完成"
        except Exception as e:
            self._status = "failed"
            self._progress = f"爬取出错: {str(e)}"


crawler_service = NhsCrawlerService()
