"""
ManhwaVERSE — AsuraScans Scraper v12
API-first approach: all listing/metadata via api.asurascans.com
HTML scraping retained only for chapter image extraction (not available via API).
"""
import re
import json
import logging
import asyncio
from datetime import datetime, timezone
from urllib.parse import urljoin, quote, quote_plus
from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup

logger = logging.getLogger("manhwaverse.scraper_asura")

BASE_URL = "https://asurascans.com"
API_URL  = "https://api.asurascans.com"

HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         BASE_URL,
}
API_HEADERS = {
    **HEADERS,
    "Host":   "api.asurascans.com",
    "Accept": "application/json, text/plain, */*",
    "Referer": BASE_URL,
}


# ── HTTP helpers ──────────────────────────────────────────────────────────────

async def _get(url: str, headers: dict | None = None, rjson: bool = False):
    try:
        async with AsyncSession(impersonate="chrome124") as s:
            r = await s.get(url, headers=headers or HEADERS, timeout=45)
            r.raise_for_status()
            return r.json() if rjson else r.text
    except Exception as e:
        logger.error("Fetch failed %s: %s", url, e)
        return None


def _img(tag) -> str:
    if not tag: return ""
    for attr in ("src", "data-src", "data-lazy-src"):
        v = (tag.get(attr) or "").strip()
        if v and v.startswith("http"): return v
    return ""


async def fetch_image_bytes(url: str):
    try:
        async with AsyncSession(impersonate="chrome124") as s:
            r = await s.get(url,
                            headers={"Referer": BASE_URL, "User-Agent": HEADERS["User-Agent"]},
                            timeout=45)
            r.raise_for_status()
            mime = r.headers.get("content-type", "image/jpeg").split(";")[0].strip()
            return r.content, mime
    except Exception as e:
        logger.error("Image fetch failed %s: %s", url, e)
        return None, None


# ── Time helper ───────────────────────────────────────────────────────────────

def _relative_time(iso: str) -> str:
    """
    Convert ISO 8601 → compact relative label.
    Returns "" for epoch/null dates (created_at = 0001-01-01T00:00:00Z).
    """
    if not iso:
        return ""
    try:
        dt  = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        # Guard: reject null/epoch sentinel dates (year 0001) and far future
        if dt.year < 2010 or dt.year > now.year + 1:
            return ""
        secs = int((now - dt).total_seconds())
        if secs < 0:    return "just now"
        if secs < 60:   return f"{secs}s ago"
        m = secs // 60
        if m < 60:      return f"{m} min ago"
        h = m // 60
        if h < 24:      return f"{h}h ago"
        d = h // 24
        if d < 30:      return f"{d}d ago"
        mo = d // 30
        if mo < 12:     return f"{mo}mo ago"
        y = mo // 12;   return f"{y}y ago"
    except Exception:
        return ""


# ── Series → dict converter ───────────────────────────────────────────────────

def _series_to_dict(item: dict, manga_url: str | None = None) -> dict:
    """
    Convert a raw API series object to the standard ManhwaVERSE manga dict.

    Chapter URLs are built as:
        {manga_url}/chapter/{chapter_number}
    e.g. https://asurascans.com/comics/nano-machine-19cdf401/chapter/317
    """
    pub = item.get("public_url", "")
    m_url = manga_url or (urljoin(BASE_URL, pub) if pub else "")

    chapters = []
    for ch in item.get("latest_chapters", [])[:3]:
        if not isinstance(ch, dict):
            continue
        ch_num = ch.get("number")
        if ch_num is None:
            continue

        ch_title = f"Chapter {ch_num}"
        ch_url   = f"{m_url}/chapter/{ch_num}" if m_url else ""
        # ONLY use published_at — created_at is always epoch (0001-01-01) → skip
        ch_date  = _relative_time(ch.get("published_at") or "")

        chapters.append({"title": ch_title, "url": ch_url, "date": ch_date})

    return {
        "title":    (item.get("title") or "").strip(),
        "url":      m_url,
        "cover":    item.get("cover") or "",
        "source":   "asurascans",
        "slug":     item.get("slug") or "",
        "chapters": chapters,
        "rating":   item.get("rating"),
        "status":   item.get("status") or "",
    }


def _extract_slug(manga_url: str) -> str:
    """
    Extract the clean API slug from a manga URL.

    public_url path: /comics/nano-machine-19cdf401
    slug field:      nano-machine
    We strip the 8-char hex suffix from the URL path segment.
    """
    seg = manga_url.rstrip("/").split("/")[-1]   # e.g. "nano-machine-19cdf401"
    return re.sub(r"-[0-9a-f]{8}$", "", seg)     # → "nano-machine"


# ── Latest Updates (API, offset-based pagination) ─────────────────────────────

async def get_latest(page: int = 1) -> list[dict]:
    """
    GET /api/series?offset=N
    Page 1 = offset 0, page 2 = offset 20, etc.
    Default API sort is by last_chapter_at descending (latest updates first).
    """
    offset = (page - 1) * 20
    url    = f"{API_URL}/api/series?offset={offset}"
    data   = await _get(url, headers=API_HEADERS, rjson=True)

    if not isinstance(data, dict) or not data.get("data"):
        logger.warning("get_latest page=%s returned no data", page)
        return []

    return [_series_to_dict(item) for item in data["data"]]


# ── Popular / Trending (API, sort=popular) ────────────────────────────────────

async def get_popular() -> list[dict]:
    """
    GET /api/series?sort=popular
    Returns series ordered by popularity_rank ascending (rank 1 = most popular).
    """
    url  = f"{API_URL}/api/series?sort=popular"
    data = await _get(url, headers=API_HEADERS, rjson=True)

    if not isinstance(data, dict) or not data.get("data"):
        logger.warning("get_popular returned no data")
        return []

    return [_series_to_dict(item) for item in data["data"][:20]]


# ── Search (API) ──────────────────────────────────────────────────────────────

async def search(query: str) -> list[dict]:
    url  = f"{API_URL}/api/search?q={quote_plus(query)}"
    data = await _get(url, headers=API_HEADERS, rjson=True)

    if not isinstance(data, dict) or "data" not in data:
        return []

    results = []
    for item in data["data"]:
        pub = item.get("public_url", "")
        if not pub:
            continue
        m_url = urljoin(BASE_URL, pub)
        results.append({
            "title":          (item.get("title") or "").strip(),
            "url":            m_url,
            "cover":          item.get("cover") or "",
            "latest_chapter": "",
            "source":         "asurascans",
        })
    return results


# ── Manga detail (HTML for description + API for chapters list) ───────────────

async def get_manga(manga_url: str) -> dict:
    """
    Fetches manga detail:
    - HTML → title, cover, description (API description has HTML entities)
    - API  → full chapter list via /api/series/{slug}/chapters
    Both requests run concurrently.
    """
    slug = _extract_slug(manga_url)

    # Run HTML fetch and API chapters fetch concurrently
    html_result, chapters_result = await asyncio.gather(
        _get(manga_url),
        _get_chapters_from_api(slug, manga_url),
        return_exceptions=True,
    )

    html     = html_result     if not isinstance(html_result, Exception)     else None
    chapters = chapters_result if not isinstance(chapters_result, Exception) else []

    # ── Parse HTML for title / cover / description ──
    bs = BeautifulSoup(html or "", "html.parser")

    h1    = bs.find("h1") or bs.find("h2")
    title = h1.text.strip() if h1 else ""

    cover = ""
    ptag  = bs.select_one("div.rounded-xl.z-0.w-full.h-full.absolute.top-0.left-0")
    if ptag and (img_tag := ptag.find_next("img")):
        cover = _img(img_tag)

    desc_node   = bs.select_one("div.mt-3.relative")
    desc_node   = desc_node.find_next("p") if desc_node else None
    description = desc_node.text.strip()[:600] if desc_node else ""

    return {
        "title":       title,
        "cover":       cover,
        "description": description,
        "chapters":    chapters,
        "source":      "asurascans",
    }


async def _get_chapters_from_api(slug: str, manga_url: str) -> list[dict]:
    """
    GET /api/series/{slug}/chapters
    Returns ALL chapters for a series, newest first.
    """
    if not slug:
        return []

    url  = f"{API_URL}/api/series/{slug}/chapters?perPage=500"
    data = await _get(url, headers=API_HEADERS, rjson=True)

    if not isinstance(data, dict) or not data.get("data"):
        logger.warning("_get_chapters_from_api slug=%s returned no data", slug)
        return []

    chapters = []
    for ch in data["data"]:
        if not isinstance(ch, dict):
            continue
        ch_num = ch.get("number")
        if ch_num is None:
            continue

        ch_num_str = str(ch_num)
        ch_title   = f"Chapter {ch_num_str}"
        ch_url     = f"{manga_url}/chapter/{ch_num_str}"
        ch_date    = _relative_time(ch.get("published_at") or "")

        # Extract number for reader compatibility
        chapters.append({
            "number": ch_num_str,
            "title":  ch_title,
            "url":    ch_url,
            "date":   ch_date,
        })

    return chapters


# ── Chapter images (HTML scraping — only thing not in API) ────────────────────

async def get_chapter(chapter_url: str) -> dict:
    """
    Scrape chapter page for image URLs.
    Chapter URL format: https://asurascans.com/comics/{slug-hash}/chapter/{number}
    """
    html = await _get(chapter_url)
    if not html:
        return {"title": "", "chapter": "", "images": [], "source": "asurascans"}

    bs = BeautifulSoup(html, "html.parser")

    h1    = bs.find("h1") or bs.find("h2")
    title = h1.text.strip() if h1 else ""

    ch_match = (re.search(r"chapter[-\s]?([\d.]+)", title, re.IGNORECASE) or
                re.search(r"chapter[-\s]?([\d.]+)", chapter_url, re.IGNORECASE))
    ch_num   = ch_match.group(1) if ch_match else ""

    images = []

    # Method 1: astro-island props (primary — most reliable)
    for astro in bs.find_all("astro-island"):
        props_str = astro.get("props")
        if not isinstance(props_str, str):
            continue
        try:
            props = _clean_astro(props_str)
        except Exception:
            continue
        if not props or "pages" not in props:
            continue
        for img_group in props["pages"]:
            if not isinstance(img_group, list):
                continue
            for img_item in img_group:
                try:
                    if not isinstance(img_item[1], dict):
                        continue
                    url_val = img_item[1].get("url")
                    if url_val and isinstance(url_val, list) and url_val:
                        images.append(quote(url_val[-1], safe=":/%?=&#+"))
                except Exception:
                    continue
        if images:
            break

    # Method 2: regular img tags fallback
    if not images:
        for img in bs.select(
            "div#readerarea img, div.reading-content img, "
            "main img, article img, section img"
        ):
            src = _img(img)
            if src:
                images.append(quote(src, safe=":/%?=&#+"))

    return {"title": title, "chapter": ch_num, "images": images, "source": "asurascans"}


def _clean_astro(props_str: str):
    while True:
        try:
            return json.loads(props_str)
        except Exception:
            if "&quot;" not in props_str:
                raise
            props_str = props_str.replace("&quot;", '"')