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
    """使用 Qwen3 批量清洗所有文章内容，提取结构化信息"""
    if not articles_data:
        return []

    texts = []
    for i, ad in enumerate(articles_data):
        raw = _extract_raw_text(ad)
        if not raw or len(raw) < 50:
            texts.append(f"[{i}] (内容不足)")
        else:
            texts.append(f"[{i}]\n{raw[:5000]}")

    batch_text = "\n\n=====\n\n".join(texts)

    prompt = f"""以下是从政府网站抓取到的 {len(articles_data)} 篇文章的网页正文内容，每篇文章以 [数字] 标记开头，可能仍混有少量无关信息。

请对每篇文章分别提取结构化信息，规则：
- title: 文章标题
- publish_date: 发布日期（如果有，格式 YYYY-MM-DD）
- source: 发布单位/来源（如果有）
- body: 正文内容（只保留文章主体文字，去掉导航、页眉、页脚、侧边栏、搜索框、版权声明、分享按钮、广告等所有无关内容）

要求：
- 每篇文章独立处理
- 如果某字段不存在则设为空字符串 ""
- body 保留原文的自然段落结构，不要省略
- 只返回 JSON 数组，不要任何其他文字

内容如下：
{batch_text}

严格按以下 JSON 格式返回（不要 markdown 代码块标记，只返回纯 JSON）：
[{{"title": "...", "publish_date": "...", "source": "...", "body": "..."}}, ...]"""

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
                    "max_tokens": 8192,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"].strip()
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
            cleaned_list = json.loads(content)
            if isinstance(cleaned_list, list):
                logger.info("[清洗] AI 批量清洗完成: %d 篇", len(cleaned_list))
                return cleaned_list
            return []
        except Exception as e:
            logger.warning("[清洗] AI 批量清洗失败: %s", e)
            return []


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


# ==================== 主服务 ====================

class GovCrawlerService:
    """政府网站列表页文章爬取服务"""

    def __init__(self):
        self._status = "idle"
        self._progress = ""
        self._target_url: str = ""
        self._results: list = []
        self._summary: str = ""

    @property
    def status(self) -> str:
        return self._status

    @property
    def progress(self) -> str:
        return self._progress

    def get_results(self) -> list:
        return self._results

    def get_summary(self) -> str:
        return self._summary

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

                # ---- AI 内容清洗 ----
                self._progress = "正在 AI 清洗文章内容..."
                cleaned = await ai_clean_all_contents(batch_results)

                merged = []
                for i, info in enumerate(articles_info):
                    item = {"title": info["title"], "url": info["url"], "content": None}
                    if i < len(batch_results):
                        item["content"] = batch_results[i]
                    if i < len(cleaned):
                        item["cleaned"] = cleaned[i]
                    merged.append(item)

                self._results = merged
                self._status = "finished"
                self._progress = f"爬取完成: 共 {len(merged)} 篇文章"

                # ---- AI 摘要 ----
                if merged:
                    self._progress = "正在生成 AI 摘要..."
                    titles = [m.get("cleaned", {}).get("title", m["title"]) for m in merged]
                    self._summary = await ai_generate_summary(titles)
                    self._progress = f"全部完成: {len(merged)} 篇文章 + AI 摘要"

            except Exception as e:
                self._status = "failed"
                self._progress = f"爬取出错: {e}"
                logger.exception("爬取过程发生异常")


crawler_service = GovCrawlerService()
