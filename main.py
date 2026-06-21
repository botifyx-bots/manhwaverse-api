"""PornhwaFlix — FastAPI Backend v11 (manga18fx + asurascans + disk image cache + pagination)"""

import os
import asyncio
import logging
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, FileResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
logger = logging.getLogger("pornhwaflix")

app = FastAPI(title="PornhwaFlix API", version="11.0.0")
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])

from scraper18fx import (
    search      as search18fx,
    get_manga   as get_manga18fx,
    get_chapter as get_chapter18fx,
    fetch_image_bytes as fetch18fx,
)
from scraperAsura import (
    search           as search_asura,
    get_manga        as get_manga_asura,
    get_chapter      as get_chapter_asura,
    get_latest       as get_latest_asura,   # API offset pagination
    get_popular      as get_popular_asura,  # API sort=popular
    fetch_image_bytes as fetchAsura,
)
import image_cache as cache
import scraper18fx

ASURA_DOMAINS = (
    "asurascans",
    "asuracomic",
    "gg.asura"
)

# ─── Startup / Shutdown ──────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    logger.info("PornhwaFlix v11 started. Cache: %s", cache.cache_stats())
    asyncio.create_task(_periodic_flush())

async def _periodic_flush():
    while True:
        await asyncio.sleep(300)
        try:
            await cache.flush_index()
            logger.debug("Cache index flushed. Stats: %s", cache.cache_stats())
        except Exception as e:
            logger.error("Periodic flush error: %s", e)

@app.on_event("shutdown")
async def shutdown():
    await cache.flush_index()
    try:
        if (
            scraper18fx._IMAGE_SESSION
            and not scraper18fx._IMAGE_SESSION.closed
        ):
            await scraper18fx._IMAGE_SESSION.close()
    except Exception:
        pass
    logger.info("Cache index saved on shutdown.")


# ─── Image proxy + cache ─────────────────────────────────────────────────────
@app.get("/api/image")
async def image_proxy(url: str):
    cached = cache.get_cached(url)
    if cached:
        content, mime = cached
        return Response(
            content=content,
            media_type=mime,
            headers={"X-Cache": "HIT", "Cache-Control": "public,max-age=31536000,immutable"},
        )

    if any(x in url.lower() for x in ASURA_DOMAINS):
        content, mime = await fetchAsura(url)
    else:
        content, mime = await fetch18fx(url)
    if not content:
        raise HTTPException(status_code=404, detail="Image not found")

    asyncio.create_task(cache.put_cached(url, content, mime or "image/jpeg"))

    return Response(
        content=content,
        media_type=mime,
        headers={"X-Cache": "MISS", "Cache-Control": "public,max-age=31536000,immutable"},
    )


# ─── Cache stats ─────────────────────────────────────────────────────────────
@app.get("/api/cache/stats")
async def api_cache_stats():
    return cache.cache_stats()


# ─── Home feed (AsuraScans only, with pagination) ────────────────────────────
@app.get("/api/latest")
async def api_latest(page: int = Query(default=1, ge=1)):
    """
    Latest updated series. Backed by AsuraScans API with offset pagination.
    Returns { data: [...], page: N, total_pages: N }
    """
    try:
        data = await get_latest_asura(page)
        asyncio.create_task(_precache_covers(data))
        # API total is 332, perPage 20 → 17 pages
        return {"data": data, "page": page, "total_pages": 17}
    except Exception as e:
        logger.error("Latest feed error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/popular")
async def api_popular():
    """Trending series via API sort=popular."""
    try:
        data = await get_popular_asura()
        asyncio.create_task(_precache_covers(data))
        return data
    except Exception as e:
        logger.error("Popular feed error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


# ─── Manga18FX ────────────────────────────────────────────────────────────────
@app.get("/api/search18fx")
async def api_search18fx(q: str = Query(default="")):
    try:
        data = await search18fx(q)
        asyncio.create_task(_precache_covers(data))
        return data
    except Exception as e:
        logger.error("18fx search error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))

@app.get("/api/manga18fx")
async def api_manga18fx(url: str = Query(...)):
    try:
        data = await get_manga18fx(url)
        asyncio.create_task(_precache_single_cover(data.get("cover", "")))
        return data
    except Exception as e:
        logger.error("18fx manga error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))

@app.get("/api/chapter18fx")
async def api_chapter18fx(url: str = Query(...)):
    try:
        data = await get_chapter18fx(url)
        asyncio.create_task(_precache_pages(data.get("images", [])))
        return data
    except Exception as e:
        logger.error("18fx chapter error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


# ─── AsuraScans ───────────────────────────────────────────────────────────────
@app.get("/api/searchAsura")
async def api_search_asura(q: str = Query(default="")):
    try:
        data = await search_asura(q)
        asyncio.create_task(_precache_covers(data))
        return data
    except Exception as e:
        logger.error("Asura search error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))

@app.get("/api/mangaAsura")
async def api_manga_asura(url: str = Query(...)):
    try:
        data = await get_manga_asura(url)
        asyncio.create_task(_precache_single_cover(data.get("cover", "")))
        return data
    except Exception as e:
        logger.error("Asura manga error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))

@app.get("/api/chapterAsura")
async def api_chapter_asura(url: str = Query(...)):
    try:
        data = await get_chapter_asura(url)
        asyncio.create_task(_precache_pages(data.get("images", [])))
        return data
    except Exception as e:
        logger.error("Asura chapter error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


# ─── Background precache helpers ─────────────────────────────────────────────
async def _precache_single_cover(url: str):
    if not url:
        return
    if cache.get_cached(url):
        return
    if any(x in url.lower() for x in ASURA_DOMAINS):
        content, mime = await fetchAsura(url)
    else:
        content, mime = await fetch18fx(url)
    if content:
        await cache.put_cached(url, content, mime or "image/jpeg")

async def _precache_covers(items: list):
    for item in items:
        cover = item.get("cover", "")
        if cover:
            asyncio.create_task(_precache_single_cover(cover))
        await asyncio.sleep(0)

async def _precache_pages(urls: list):
    BATCH = 4
    for i in range(0, len(urls), BATCH):
        batch = urls[i:i+BATCH]
        tasks = []
        for url in batch:
            if not cache.get_cached(url):
                tasks.append(_precache_single_cover(url))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0.05)


# ─── Frontend ─────────────────────────────────────────────────────────────────
FRONTEND = os.path.join(os.path.dirname(__file__), "..", "frontend")

if os.path.exists(FRONTEND):
    app.mount(
        "/static",
        StaticFiles(directory=os.path.join(FRONTEND, "assets")),
        name="static",
    )

    @app.get("/")
    async def serve_index():
        return FileResponse(os.path.join(FRONTEND, "index.html"))

    @app.get("/search")
    async def serve_search():
        return FileResponse(os.path.join(FRONTEND, "search.html"))

    @app.get("/manga")
    async def serve_manga():
        return FileResponse(os.path.join(FRONTEND, "manga.html"))

    @app.get("/reader")
    async def serve_reader():
        return FileResponse(os.path.join(FRONTEND, "reader.html"))

    @app.get("/library")
    async def serve_library():
        return FileResponse(os.path.join(FRONTEND, "library.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)