import asyncio
import os

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from crawler import crawler_service

app = FastAPI(title="NHSA Crawler", description="国家医保局网站内容爬取服务")

static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

_crawl_lock = asyncio.Lock()


@app.get("/")
async def root():
    return FileResponse(os.path.join(static_dir, "test.html"))


@app.post("/api/crawl")
async def start_crawl():
    if crawler_service.status == "running":
        return JSONResponse(
            {"status": "busy", "message": "爬取任务正在执行中，请等待完成"},
            status_code=409,
        )
    async with _crawl_lock:
        asyncio.create_task(crawler_service.crawl_all())
    return {"status": "started", "message": "爬取任务已启动"}


@app.get("/api/status")
async def get_status():
    return {
        "status": crawler_service.status,
        "progress": crawler_service.progress,
    }


@app.get("/api/results")
async def get_results(section: str = Query(None, description="栏目名称: yibao_zhengce / dongtai / tongji_shuju")):
    if crawler_service.status in ("idle", "running"):
        return {
            "status": crawler_service.status,
            "progress": crawler_service.progress,
            "results": None,
        }
    if section:
        if section not in ("yibao_zhengce", "dongtai", "tongji_shuju"):
            raise HTTPException(status_code=400, detail="无效的栏目名称")
        data = crawler_service.get_results(section)
    else:
        data = crawler_service.get_results()
    return {
        "status": crawler_service.status,
        "progress": crawler_service.progress,
        "results": data,
    }


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=7108, reload=True)
