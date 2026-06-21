"""
Manhwaflix — Image Cache + Fetch Engine
Stores image bytes on disk keyed by URL hash.
Index (URL→file, mime, size, ts) lives in a single JSON file.
Max storage: 300 GB. LRU eviction when approaching limit.
Retry logic: exponential back-off with jitter, configurable per-source headers.
"""

import os
import re
import json
import time
import random
import hashlib
import asyncio
import logging
from pathlib import Path
from typing import Optional, Tuple

import aiohttp

logger = logging.getLogger("manhwaflix.cache")

# ── Config ──────────────────────────────────────────────────────────────────
BASE_DIR   = Path(os.environ.get("MFX_CACHE_DIR", "/var/cache/manhwaflix"))
IMG_DIR    = BASE_DIR / "images"
INDEX_FILE = BASE_DIR / "index.json"

MAX_CACHE_BYTES = int(os.environ.get("MFX_MAX_CACHE_GB", "300")) * 1024 ** 3
EVICT_TARGET    = int(MAX_CACHE_BYTES * 0.90)

# Retry config
MAX_RETRIES     = 4
BASE_BACKOFF_S  = 0.8   # first retry after ~0.8 s, doubles each time + jitter
FETCH_TIMEOUT_S = 22

# ── Per-source headers ───────────────────────────────────────────────────────
_HEADERS_18FX = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Accept":          "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer":         "https://manga18fx.com/",
    "Origin":          "https://manga18fx.com",
}
_HEADERS_ASURA = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Accept":          "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer":         "https://asurascans.com/",
    "Origin":          "https://asurascans.com",
}
_HEADERS_DEFAULT = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Accept":     "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}

def _headers_for(url: str) -> dict:
    if "manga18fx" in url or "img0" in url:
        return _HEADERS_18FX
    if "asurascans" in url or "asura" in url.lower():
        return _HEADERS_ASURA
    return _HEADERS_DEFAULT


# ── In-memory index ──────────────────────────────────────────────────────────
_index_lock  = asyncio.Lock()
_index:       dict = {}
_total_bytes: int  = 0
_write_count: int  = 0   # flush every N writes


def _hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()

def _ext_from_mime(mime: str) -> str:
    return {
        "image/jpeg": ".jpg",
        "image/png":  ".png",
        "image/gif":  ".gif",
        "image/webp": ".webp",
        "image/avif": ".avif",
    }.get(mime, ".jpg")

def _ext_from_url(url: str) -> str:
    path = url.split("?")[0].rstrip("/")
    m = re.search(r"\.(webp|avif|png|gif|jpe?g)$", path, re.I)
    return "." + m.group(1).lower() if m else ".jpg"


# ── Init / Persist ────────────────────────────────────────────────────────────
def _load_index():
    global _index, _total_bytes
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    if INDEX_FILE.exists():
        try:
            with open(INDEX_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            _index       = data.get("entries", {})
            _total_bytes = data.get("total_bytes", 0)
            logger.info("✅ Cache loaded — %d entries | %.2f GB used",
                        len(_index), _total_bytes / 1024**3)
        except Exception as e:
            logger.warning("⚠️  Cache index corrupt, resetting: %s", e)
            _index, _total_bytes = {}, 0
    else:
        logger.info("📂 Cache index not found — starting fresh")


def _save_index_sync():
    try:
        tmp = INDEX_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"entries": _index, "total_bytes": _total_bytes,
                       "updated": time.time()}, f, separators=(",", ":"))
        tmp.replace(INDEX_FILE)
    except Exception as e:
        logger.error("❌ Failed to save cache index: %s", e)

async def _save_index():
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _save_index_sync)


# ── LRU Eviction ──────────────────────────────────────────────────────────────
def _evict_lru():
    global _total_bytes
    if _total_bytes <= MAX_CACHE_BYTES:
        return
    logger.warning("⚠️  Cache full (%.2f GB) — evicting LRU entries…",
                   _total_bytes / 1024**3)
    sorted_keys = sorted(_index.keys(), key=lambda k: _index[k].get("ts", 0))
    evicted = 0
    for key in sorted_keys:
        if _total_bytes <= EVICT_TARGET:
            break
        entry = _index.pop(key, None)
        if not entry:
            continue
        try:
            (IMG_DIR / entry["filename"]).unlink(missing_ok=True)
        except Exception:
            pass
        _total_bytes -= entry.get("size", 0)
        evicted += 1
    logger.info("🗑️  Evicted %d entries — cache now %.2f GB", evicted, _total_bytes / 1024**3)


# ── Core Fetch with Retry ─────────────────────────────────────────────────────
async def fetch_image_bytes(url: str) -> Tuple[Optional[bytes], Optional[str]]:
    """
    Fetch image from origin with exponential back-off retry.
    Returns (bytes, mime_type) or (None, None) on total failure.
    """
    headers = _headers_for(url)
    timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT_S, connect=8)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with aiohttp.ClientSession(headers=headers) as sess:
                async with sess.get(url, timeout=timeout, allow_redirects=True) as resp:
                    if resp.status == 200:
                        content = await resp.read()
                        mime    = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
                        if attempt > 1:
                            logger.info("✅ Fetch OK after %d attempts — %s", attempt, url)
                        return content, mime

                    if resp.status in (403, 404):
                        # No point retrying hard 4xx
                        logger.warning("🚫 HTTP %d — giving up on %s", resp.status, url)
                        return None, None

                    logger.warning("⚠️  HTTP %d on attempt %d/%d — %s",
                                   resp.status, attempt, MAX_RETRIES, url)

        except asyncio.TimeoutError:
            logger.warning("⏱️  Timeout on attempt %d/%d — %s", attempt, MAX_RETRIES, url)
        except aiohttp.ClientError as e:
            logger.warning("🌐 Network error attempt %d/%d — %s: %s", attempt, MAX_RETRIES, url, e)
        except Exception as e:
            logger.error("❌ Unexpected error attempt %d/%d — %s: %s", attempt, MAX_RETRIES, url, e)

        if attempt < MAX_RETRIES:
            backoff = BASE_BACKOFF_S * (2 ** (attempt - 1)) + random.uniform(0, 0.4)
            logger.info("🔁 Retry %d in %.1fs — %s", attempt + 1, backoff, url)
            await asyncio.sleep(backoff)

    logger.error("💀 All %d attempts failed — %s", MAX_RETRIES, url)
    return None, None


# ── Public Cache API ──────────────────────────────────────────────────────────
def get_cached(url: str) -> Optional[Tuple[bytes, str]]:
    """Return (bytes, mime) from disk cache, or None on miss."""
    key   = _hash(url)
    entry = _index.get(key)
    if not entry:
        return None
    path = IMG_DIR / entry["filename"]
    if not path.exists():
        _index.pop(key, None)
        return None
    try:
        data       = path.read_bytes()
        entry["ts"] = time.time()
        return data, entry.get("mime", "image/jpeg")
    except Exception as e:
        logger.error("❌ Cache read error %s: %s", path, e)
        return None


async def put_cached(url: str, content: bytes, mime: str) -> None:
    """Write image to disk cache (thread-safe, non-blocking)."""
    global _total_bytes, _write_count

    key  = _hash(url)
    ext  = _ext_from_mime(mime) or _ext_from_url(url)
    name = f"{key}{ext}"
    path = IMG_DIR / name

    async with _index_lock:
        if key in _index and path.exists():
            _index[key]["ts"] = time.time()
            return

        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, path.write_bytes, content)
        except Exception as e:
            logger.error("❌ Cache write error %s: %s", path, e)
            return

        size = len(content)
        if key in _index:
            _total_bytes -= _index[key].get("size", 0)

        _index[key] = {"filename": name, "mime": mime, "size": size,
                       "ts": time.time(), "url": url}
        _total_bytes += size
        _write_count += 1

        if _total_bytes > MAX_CACHE_BYTES:
            _evict_lru()

        if _write_count % 40 == 0:
            await _save_index()


async def flush_index() -> None:
    async with _index_lock:
        await _save_index()


def cache_stats() -> dict:
    return {
        "entries":   len(_index),
        "total_gb":  round(_total_bytes / 1024**3, 3),
        "max_gb":    round(MAX_CACHE_BYTES / 1024**3, 1),
        "usage_pct": round(_total_bytes / MAX_CACHE_BYTES * 100, 2),
        "cache_dir": str(BASE_DIR),
    }


_load_index()