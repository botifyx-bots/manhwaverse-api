"""
PornhwaFlix — Manga18FX Scraper
Uses aiohttp (no Cloudflare on this site) for fast requests.
"""
import asyncio
import re
import logging
from urllib.parse import urljoin, quote_plus, quote
import aiohttp
from bs4 import BeautifulSoup

logger = logging.getLogger("pornhwaflix.scraper18fx")
_IMAGE_SESSION = None
BASE_URL = "https://manga18fx.com/"
HEADERS  = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE_URL,
}

_IMAGE_HEADERS = {
    "User-Agent":  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Accept":      "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Referer":     BASE_URL,
    "Accept-Encoding": "gzip, deflate, br",
}


async def fetch_image_bytes(url: str):

    try:

        session = await get_image_session()

        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=30),
            allow_redirects=True
        ) as resp:

            if resp.status != 200:
                logger.warning(
                    "Image %s returned %d",
                    url,
                    resp.status
                )
                return None, None

            content = await resp.read()

            mime = (
                resp.headers
                .get("content-type", "image/jpeg")
                .split(";")[0]
                .strip()
            )

            return content, mime

    except Exception as e:

        logger.error(
            "Image fetch failed %s: %s",
            url,
            e
        )

        return None, None

async def get_image_session():
    global _IMAGE_SESSION

    if (
        _IMAGE_SESSION is None
        or _IMAGE_SESSION.closed
    ):

        connector = aiohttp.TCPConnector(
            limit=200,
            limit_per_host=100,
            ttl_dns_cache=300,
        )

        _IMAGE_SESSION = aiohttp.ClientSession(
            headers=_IMAGE_HEADERS,
            connector=connector,
        )

    return _IMAGE_SESSION

async def _get(url: str) -> str:
    async with aiohttp.ClientSession(headers=HEADERS) as s:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=30), allow_redirects=True) as r:
            r.raise_for_status()
            return await r.text()


def _img(tag) -> str:
    if not tag: return ""
    for a in ("src", "data-src", "data-lazy-src", "data-original"):
        v = tag.get(a, "").strip()
        if v and v.startswith("http"): return v
    return ""


def _is_raw(url: str) -> bool:
    """Return True if the manga URL is a raw (untranslated) version."""
    slug = url.rstrip("/").split("/")[-1].lower()
    return slug.endswith("-raw")


# ── Search ────────────────────────────────────────────────────────────────────
async def search(query: str) -> list[dict]:
    url  = f"https://manga18fx.com/search?q={quote_plus(query)}"
    html = await _get(url)
    bs   = BeautifulSoup(html, "html.parser")
    cont = bs.find("div", {"class": "listupd"})
    if not cont: return []
    results = []
    for card in cont.find_all("div", {"class": "thumb-manga"}):
        a   = card.find("a")
        img = card.find("img")
        if not a: continue
        manga_url = urljoin(BASE_URL, a.get("href", ""))
        # Skip raw manga
        if _is_raw(manga_url):
            continue
        results.append({
            "title":          a.get("title", "").strip() or a.text.strip(),
            "url":            manga_url,
            "cover":          _img(img),
            "latest_chapter": "",
            "source":         "manga18fx",
        })
    return results


# ── Manga detail ─────────────────────────────────────────────────────────────
async def get_manga(manga_url: str) -> dict:
    html = await _get(manga_url)
    bs   = BeautifulSoup(html, "html.parser")

    h1    = bs.find("h1") or bs.find("h2")
    title = h1.text.strip() if h1 else ""

    cover = ""
    for cls in ("summary_image", "thumb"):
        node = bs.find("div", {"class": cls}) or bs.find("div", {"class": lambda c: c and cls in c})
        if node:
            cover = _img(node.find("img"))
            if cover: break

    desc_node = bs.find(class_="dsct") or bs.find(class_="summary__content") or bs.find(class_="entry-content")
    description = desc_node.get_text(" ", strip=True)[:600] if desc_node else ""

    chapters = []
    ul = bs.find("ul", {"class": "row-content-chapter"})
    if ul:
        for li in ul.find_all("li", {"class": "a-h"}):
            a_tag = li.find("a")
            if not a_tag: continue
            ch_url = urljoin(BASE_URL, a_tag.get("href", ""))
            title_ = a_tag.text.strip()
            match  = re.search(r"[\d.]+", title_)
            num    = match.group(0) if match else str(len(chapters)+1)
            date_span = li.find("span", {"class": re.compile("date|time", re.I)})
            chapters.append({
                "number": num,
                "title":  title_,
                "url":    ch_url,
                "date":   date_span.text.strip() if date_span else "",
            })

    return {
        "title":       title,
        "cover":       cover,
        "description": description,
        "chapters":    chapters,
        "source":      "manga18fx",
    }


# ── Chapter images ────────────────────────────────────────────────────────────
async def get_chapter(chapter_url: str) -> dict:
    html = await _get(chapter_url)
    bs   = BeautifulSoup(html, "html.parser")

    h1    = bs.find("h1") or bs.find("h2")
    title = h1.text.strip() if h1 else ""

    ch_match = re.search(r"chapter[-\s]?([\d.]+)", title, re.IGNORECASE) or \
               re.search(r"chapter[-\s]?([\d.]+)", chapter_url, re.IGNORECASE)
    chapter_num = ch_match.group(1) if ch_match else ""

    images = []
    for card in bs.find_all("div", {"class": "page-break"}):
        img = card.find("img")
        src = _img(img)
        if src: images.append(quote(src, safe=":/%?=&#+"))

    # Sort pages by the leading number in the filename (e.g. "7-6241c.jpg" → 7)
    # so they always arrive in sequence regardless of DOM order.


    return {"title": title, "chapter": chapter_num, "images": images, "source": "manga18fx"}

# ── Latest Updates (homepage) ─────────────────────────────────────────────────
async def get_latest() -> list[dict]:
    html = await _get(BASE_URL)
    bs   = BeautifulSoup(html, "html.parser")
    results = []

    for block in bs.select("div.utao"):
        a   = block.select_one("div.uta a")
        img = block.select_one("div.uta img")
        if not a: continue
        manga_url = urljoin(BASE_URL, a.get("href", ""))
        if _is_raw(manga_url): continue
        title = (a.get("title") or a.text).strip()

        chapters = []
        for ch_a in block.select("div.lstemp a")[:3]:
            span = ch_a.find_next_sibling("span")
            chapters.append({
                "title": ch_a.text.strip(),
                "url":   urljoin(BASE_URL, ch_a.get("href", "")),
                "date":  span.text.strip() if span else "",
            })

        results.append({"title": title, "url": manga_url, "cover": _img(img),
                        "source": "manga18fx", "chapters": chapters, "rating": None})
    return results[:20]


# ── Popular / Trending ────────────────────────────────────────────────────────
async def get_popular() -> list[dict]:
    html = await _get("https://manga18fx.com/?m_orderby=trending")
    bs   = BeautifulSoup(html, "html.parser")
    results = []
    for card in bs.select("div.thumb-manga"):
        a   = card.find("a")
        img = card.find("img")
        if not a: continue
        manga_url = urljoin(BASE_URL, a.get("href", ""))
        if _is_raw(manga_url): continue
        results.append({
            "title":  (a.get("title") or a.text).strip(),
            "url":    manga_url,
            "cover":  _img(img),
            "source": "manga18fx",
            "rating": None,
        })
    return results[:15]