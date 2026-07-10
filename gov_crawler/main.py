import asyncio
import os

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from crawler import crawler_service, PRESET_SOURCES

app = FastAPI(title="Gov Crawler", description="政府网站列表页文章爬取服务")

static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

_crawl_lock = asyncio.Lock()


class CrawlRequest(BaseModel):
    url: str


@app.get("/")
async def root():
    return FileResponse(os.path.join(static_dir, "test.html"))


@app.get("/api/sources")
async def get_sources():
    """获取预设站点列表"""
    return {"sources": PRESET_SOURCES}


@app.post("/api/crawl")
async def start_crawl(req: CrawlRequest):
    """启动爬取，传入目标 URL"""
    if not req.url or not req.url.startswith("http"):
        raise HTTPException(status_code=400, detail="请提供有效的 URL")

    if crawler_service.status == "running":
        return JSONResponse(
            {"status": "busy", "message": "爬取任务正在执行中，请等待完成"},
            status_code=409,
        )
    async with _crawl_lock:
        asyncio.create_task(crawler_service.crawl_url(req.url))
    return {"status": "started", "message": "爬取任务已启动", "url": req.url}


@app.get("/api/status")
async def get_status():
    return {
        "status": crawler_service.status,
        "progress": crawler_service.progress,
    }


@app.get("/api/results")
async def get_results():
    if crawler_service.status in ("idle", "running"):
        return {
            "status": crawler_service.status,
            "progress": crawler_service.progress,
            "results": None,
            "summary": None,
        }
    return {
        "status": crawler_service.status,
        "progress": crawler_service.progress,
        "results": crawler_service.get_results(),
        "summary": crawler_service.get_summary(),
    }


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=7107, reload=True)
