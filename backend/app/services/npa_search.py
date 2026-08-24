"""Find official NPA URLs when SearXNG is empty or returns the wrong page.

Order: collect hits from several search backends, rewrite pravo.by cards
into full-text variants, rank, return unique allowlisted URLs.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import re
from urllib.parse import parse_qs, unquote, urlparse

from app.domains import host_allowed
from app.services.downloader import usable_url
from app.services.searxng_client import search_searxng

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

_HREF_RE = re.compile(r"""https?://[^\s"'<>\\]+""", re.I)
_DOC_CODE_RE = re.compile(
    r"(?:[?&]p0=|[?&]regnum=|[?&]RN=)([A-Za-z][A-Za-z0-9]{5,24})",
    re.I,
)
_PDF_CODE_RE = re.compile(r"/op/([A-Za-z][A-Za-z0-9]{5,24})(?:_\d+)?", re.I)
_QUOTED_RE = re.compile(r"[«\"„](.+?)[»\"“]")
_STOP = {
    "республики",
    "беларусь",
    "беларуси",
    "утвержден",
    "утверждено",
    "утверждении",
    "некоторых",
    "вопросах",
    "порядке",
    "проведения",
    "национальной",
    "национального",
}

NEWS_MARKERS = ("/novosti/", "/analitika/", "/news/")
MAX_CANDIDATES = 8
MAX_QUERIES = 4


def extract_doc_code(url: str | None) -> str | None:
    if not url:
        return None
    match = _DOC_CODE_RE.search(url)
    if match:
        return match.group(1)
    match = _PDF_CODE_RE.search(url)
    if match:
        return match.group(1)
    return None


def expand_official_urls(url: str | None) -> list[str]:
    """Rewrite a pravo.by card/news URL into full-text variants."""
    cleaned = usable_url(url)
    out: list[str] = []
    seen: set[str] = set()

    def add(item: str | None) -> None:
        value = usable_url(item)
        if not value or value in seen:
            return
        seen.add(value)
        out.append(value)

    add(cleaned)
    code = extract_doc_code(cleaned or url or "")
    if not code:
        return out
    add(f"https://pravo.by/document/?guid=3871&p0={code}")
    add(f"https://pravo.by/webnpa/text.asp?RN={code}")
    add(f"https://pravo.by/document/?guid=12551&p0={code}&p1=1")
    add(f"https://pravo.by/document/?guid=12551&p0={code}")
    add(f"https://etalonline.by/document/?regnum={code}")
    return out


def score_url(url: str, title: str = "", hit_title: str = "") -> int:
    low = (url or "").lower()
    score = 0
    if "guid=3871" in low:
        score += 50
    if "webnpa/text" in low:
        score += 45
    if low.endswith(".pdf") or "/upload/docs/" in low:
        score += 40
    if "guid=12551" in low:
        score += 25
    if "etalonline.by" in low and "/document/" in low:
        score += 30
    if "guid=3961" in low:
        score -= 25
    if any(marker in low for marker in NEWS_MARKERS):
        score -= 90
    blob = f"{hit_title} {title}".lower()
    score += _token_overlap(title, hit_title) * 8
    if title:
        significant = [t for t in _tokens(title) if len(t) >= 5]
        if significant and sum(1 for t in significant if t in low or t in blob) >= 2:
            score += 12
    return score


def build_search_queries(search_queries: list[str] | None, title: str | None) -> list[str]:
    expanded: list[str] = []
    title = (title or "").strip()
    if title:
        expanded.append(f"site:pravo.by {title}")
        quoted = _quoted_name(title)
        if quoted and quoted.lower() not in title.lower():
            expanded.append(f"site:pravo.by {quoted}")
        elif quoted:
            expanded.append(f"site:pravo.by {quoted}")
        low = title.lower()
        if "нбрб" in low or "национальн" in low:
            expanded.append(f"site:nbrb.by {title}")
        if "минфин" in low:
            expanded.append(f"site:minfin.gov.by {title}")
        expanded.append(f"site:etalonline.by {title}")
    for query in search_queries or []:
        q = (query or "").strip()
        if not q:
            continue
        expanded.append(q)
        if "pravo.gov.by" in q:
            expanded.append(q.replace("pravo.gov.by", "pravo.by"))
            expanded.append(q.replace("pravo.gov.by", "etalonline.by"))
        if q.lower().startswith("site:"):
            parts = q.split(" ", 1)
            if len(parts) == 2 and parts[1].strip():
                expanded.append(parts[1].strip())
    seen: set[str] = set()
    out: list[str] = []
    for item in expanded:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out[:MAX_QUERIES]


async def find_candidate_urls(
    search_queries: list[str] | None,
    title: str | None = None,
) -> list[tuple[str, str]]:
    """Allowlisted URLs, strongest first. Empty if nothing official was found."""
    queries = build_search_queries(search_queries, title)
    hits: list[tuple[str, str, str]] = []
    for query in queries:
        found = await _search_engines(query)
        hits.extend(found)
        strong = [
            url
            for url, _source, _hit_title in hits
            if score_url(url, title or "", "") >= 25
        ]
        if len(dict.fromkeys(strong)) >= 3:
            break

    ranked: list[tuple[int, str, str]] = []
    seen: set[str] = set()
    for url, source, hit_title in hits:
        for variant in expand_official_urls(url):
            if variant in seen:
                continue
            seen.add(variant)
            ranked.append((score_url(variant, title or "", hit_title), variant, source))
    ranked.sort(key=lambda row: row[0], reverse=True)
    return [(url, source) for score, url, source in ranked if score > -40][:MAX_CANDIDATES]


async def find_best_url(search_queries: list[str], title: str | None = None) -> dict | None:
    """Compatibility wrapper: first ranked candidate or None."""
    hits = await find_candidate_urls(search_queries, title=title)
    if not hits:
        return None
    return {"url": hits[0][0], "title": title or "", "source": hits[0][1]}


async def _search_engines(query: str) -> list[tuple[str, str, str]]:
    tasks = [
        _safe_hits("searxng", _from_searxng, query),
        _safe_hits("duckduckgo", _from_html_search, query, "https://html.duckduckgo.com/html/", True),
        _safe_hits("bing", _from_html_search, query, "https://www.bing.com/search", False),
    ]
    buckets = await asyncio.gather(*tasks)
    out: list[tuple[str, str, str]] = []
    for bucket in buckets:
        out.extend(bucket)
    return out


async def _safe_hits(source: str, fn, *args) -> list[tuple[str, str, str]]:
    try:
        return await fn(source, *args)
    except Exception:
        return []


async def _from_searxng(source: str, query: str) -> list[tuple[str, str, str]]:
    rows = await search_searxng(query, count=8)
    out: list[tuple[str, str, str]] = []
    for row in rows:
        url = usable_url(row.get("url"))
        if url:
            out.append((url, source, row.get("title") or ""))
    return out


async def _from_html_search(
    source: str,
    query: str,
    endpoint: str,
    as_post: bool,
) -> list[tuple[str, str, str]]:
    import httpx

    async with httpx.AsyncClient(
        timeout=20.0,
        follow_redirects=True,
        headers=BROWSER_HEADERS,
    ) as client:
        if as_post:
            resp = await client.post(endpoint, data={"q": query})
        else:
            resp = await client.get(endpoint, params={"q": query})
        resp.raise_for_status()
        body = resp.text
    return [(url, source, "") for url in _urls_from_html(body)]


def _urls_from_html(body: str) -> list[str]:
    text = html_lib.unescape(body or "")
    found: list[str] = []
    seen: set[str] = set()
    for raw in _HREF_RE.findall(text):
        url = _unwrap_redirect(raw.rstrip(").,;]"))
        cleaned = usable_url(url)
        if not cleaned or cleaned in seen:
            continue
        if not host_allowed(cleaned):
            continue
        seen.add(cleaned)
        found.append(cleaned)
    return found


def _unwrap_redirect(url: str) -> str:
    if "uddg=" in url:
        qs = parse_qs(urlparse(url).query)
        wrapped = (qs.get("uddg") or [""])[0]
        if wrapped:
            return unquote(wrapped)
    parsed = urlparse(url)
    if parsed.path.endswith("/l/") or "duckduckgo.com" in (parsed.netloc or ""):
        qs = parse_qs(parsed.query)
        for key in ("uddg", "u", "url"):
            if qs.get(key):
                return unquote(qs[key][0])
    return url


def _quoted_name(title: str) -> str:
    match = _QUOTED_RE.search(title or "")
    if match:
        return match.group(1).strip()
    return (title or "").strip()


def _tokens(text: str) -> list[str]:
    words = re.findall(r"[а-яёa-z0-9]{4,}", (text or "").lower())
    return [w for w in words if w not in _STOP]


def _token_overlap(title: str, hit_title: str) -> int:
    left = set(_tokens(title))
    right = set(_tokens(hit_title))
    if not left or not right:
        return 0
    return len(left & right)
