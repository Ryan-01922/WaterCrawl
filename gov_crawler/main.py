import asyncio
import os

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from crawler import task_manager, start_workers, PRESET_SOURCES

app = FastAPI(title="Gov Crawler", description="政府网站列表页文章爬取服务 - Redis 任务队列")

static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


class CrawlRequest(BaseModel):
    url: str


@app.on_event("startup")
async def startup():
    """启动时初始化 Redis 并启动 Worker"""
    asyncio.create_task(start_workers(), name="workers-supervisor")


@app.get("/")
async def root():
    return FileResponse(os.path.join(static_dir, "test.html"))


@app.get("/api/sources")
async def get_sources():
    """获取预设站点列表"""
    return {"sources": PRESET_SOURCES}


@app.post("/api/crawl")
async def start_crawl(req: CrawlRequest):
    """启动爬取：入队后立即返回 task_id"""
    if not req.url or not req.url.startswith("http"):
        raise HTTPException(status_code=400, detail="请提供有效的 URL")

    task_id = await task_manager.create_task(req.url)
    return {
        "status": "queued",
        "message": "爬取任务已加入队列",
        "task_id": task_id,
        "url": req.url,
    }


@app.get("/api/status")
async def get_status(task_id: str = Query(..., description="任务 ID")):
    """查询指定任务的执行状态"""
    task = await task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    return {
        "task_id": task_id,
        "status": task.get("status"),
        "progress": task.get("progress"),
        "url": task.get("url"),
        "created_at": task.get("created_at"),
    }


@app.get("/api/results")
async def get_results(task_id: str = Query(..., description="任务 ID")):
    """获取指定任务的爬取结果"""
    task = await task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    if task.get("status") in ("pending", "running"):
        return {
            "task_id": task_id,
            "status": task.get("status"),
            "progress": task.get("progress"),
            "results": None,
            "summary": None,
        }
    results = await task_manager.get_results(task_id)
    links = await task_manager.get_links(task_id)
    summary = await task_manager.get_summary(task_id)
    return {
        "task_id": task_id,
        "status": task.get("status"),
        "progress": task.get("progress"),
        "links": links,
        "total": len(links),
        "results": results,
        "summary": summary,
    }


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=7107, reload=True)
