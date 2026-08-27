"""Find official NPA URLs when SearXNG is empty or returns the wrong page.

Order: collect hits from several search backends, rewrite pravo.by cards
into full-text variants, rank, return unique allowlisted URLs.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import logging
import re
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.config import settings
from app.domains import host_allowed
from app.services.allowlist_http import allowlisted_get
from app.services.downloader import usable_url
from app.services.http_constants import BROWSER_HEADERS, NEWS_MARKERS
from app.services.searxng_client import search_searxng

logger = logging.getLogger(__name__)

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

FAMOUS_CODES = {
    "hk9800218": "гражданский кодекс",
    "hk0200166": "налоговый кодекс",
    "hk0000441": "банковский кодекс",
}
MAX_CANDIDATES = 8
MAX_QUERIES = 6
_NUM_RE = re.compile(r"№\s*(\d{1,4})", re.I)


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
    add(f"https://etalonline.by/document/?regnum={code}")
    add(f"https://pravo.by/document/?guid=12551&p0={code}&p1=1")
    add(f"https://pravo.by/document/?guid=12551&p0={code}")
    add(f"https://pravo.by/document/?guid=3871&p0={code}")
    add(f"https://pravo.by/webnpa/text.asp?RN={code}")
    return out


def score_url(url: str, title: str = "", hit_title: str = "") -> int:
    low = (url or "").lower()
    score = 0
    if "etalonline.by" in low and "/document/" in low:
        score += 55
    if "guid=3871" in low:
        score += 40
    if "webnpa/text" in low:
        score += 20
    if low.endswith(".pdf") or "/upload/docs/" in low:
        score += 45
    if "guid=12551" in low:
        score += 35
    if "guid=3961" in low:
        score -= 25
    if any(marker in low for marker in NEWS_MARKERS):
        score -= 90
    if not (hit_title or "").strip():
        score -= 40
    code = extract_doc_code(url)
    if code:
        famous = FAMOUS_CODES.get(code.lower())
        if famous and famous not in (title or "").lower():
            score -= 120
    blob = f"{hit_title} {title}".lower()
    score += _token_overlap(title, hit_title) * 8
    if hit_title and "кодекс" in hit_title.lower() and _token_overlap(title, hit_title) == 0:
        score -= 100
    if title:
        significant = [t for t in _tokens(title) if len(t) >= 5]
        if significant and sum(1 for t in significant if t in low or t in blob) >= 2:
            score += 12
    return score


def _search_phrase(title: str) -> str:
    quoted = _quoted_name(title)
    phrase = quoted or title
    phrase = re.sub(r"республики\s+беларусь", "", phrase, flags=re.I)
    phrase = re.sub(r"[«»\"„“”'`]+", " ", phrase)
    return re.sub(r"\s+", " ", phrase).strip(" .,:;")


def build_search_queries(search_queries: list[str] | None, title: str | None) -> list[str]:
    expanded: list[str] = []
    title = (title or "").strip()
    phrase = _search_phrase(title) if title else ""
    low = title.lower()
    domains: list[str] = []
    if any(token in low for token in ("нбрб", "национальн", "банк")):
        domains.append("nbrb.by")
    if any(token in low for token in ("минфин", "бухгалтер")):
        domains.append("minfin.gov.by")
    domains.extend(["pravo.by", "etalonline.by"])

    if phrase:
        for domain in domains:
            expanded.append(f"site:{domain} {phrase}")
        number = _NUM_RE.search(title)
        if number:
            expanded.append(f"site:pravo.by {phrase} № {number.group(1)}")
            expanded.append(f"site:nbrb.by {phrase} {number.group(1)}")
        if phrase != title:
            expanded.append(f"site:pravo.by {title}")
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
    if title:
        hits.extend(await _from_official_sites(title))
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
    return [(url, source) for score, url, source in ranked if score >= 20][:MAX_CANDIDATES]


_fallback_warned = False


async def _search_engines(query: str) -> list[tuple[str, str, str]]:
    global _fallback_warned
    tasks = [_safe_hits("searxng", _from_searxng, query)]
    if settings.npa_web_fallback:
        if not _fallback_warned:
            logger.warning(
                "NPA_WEB_FALLBACK is on: act titles are sent to DuckDuckGo and Bing"
            )
            _fallback_warned = True
        tasks.extend(
            [
                _safe_hits(
                    "duckduckgo",
                    _from_html_search,
                    query,
                    "https://html.duckduckgo.com/html/",
                    True,
                    None,
                ),
                _safe_hits(
                    "bing",
                    _from_html_search,
                    query,
                    "https://www.bing.com/search",
                    False,
                    {"setlang": "ru", "cc": "by", "mkt": "ru-BY"},
                ),
            ]
        )
    buckets = await asyncio.gather(*tasks)
    out: list[tuple[str, str, str]] = []
    for bucket in buckets:
        out.extend(bucket)
    return out


async def _safe_hits(source: str, fn, *args) -> list[tuple[str, str, str]]:
    try:
        return await fn(source, *args)
    except Exception as exc:  # noqa: BLE001
        logger.warning("search backend %s failed: %s", source, exc)
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
    extra_params: dict | None,
) -> list[tuple[str, str, str]]:
    params = {"q": query}
    if extra_params:
        params.update(extra_params)
    async with httpx.AsyncClient(
        timeout=12.0,
        follow_redirects=True,
        headers=BROWSER_HEADERS,
    ) as client:
        if as_post:
            resp = await client.post(endpoint, data=params)
        else:
            resp = await client.get(endpoint, params=params)
        resp.raise_for_status()
        body = resp.text
        page_url = str(resp.url)
    return _links_from_page(source, body, page_url)


async def _from_official_sites(title: str) -> list[tuple[str, str, str]]:
    phrase = _search_phrase(title)
    if not phrase:
        return []
    tasks = [
        _safe_hits(
            "pravo",
            _from_get_search,
            "https://pravo.by/pravovaya-informatsiya/pravovye-akty-po-temam/poisk-v-tbd/",
            {"p0": phrase},
        ),
        _safe_hits(
            "etalonline",
            _from_get_search,
            "https://etalonline.by/search/",
            {"search_str": phrase, "s": "1", "force": "1", "d": "1"},
        ),
        _safe_hits(
            "nbrb",
            _from_get_search,
            "https://www.nbrb.by/search",
            {"search": phrase},
        ),
    ]
    buckets = await asyncio.gather(*tasks)
    out: list[tuple[str, str, str]] = []
    for bucket in buckets:
        out.extend(bucket)
    return out


async def _from_get_search(
    source: str,
    endpoint: str,
    params: dict,
) -> list[tuple[str, str, str]]:
    async with httpx.AsyncClient(
        timeout=20.0,
        follow_redirects=False,
        headers=BROWSER_HEADERS,
    ) as client:
        resp = await allowlisted_get(client, endpoint, params=params)
        resp.raise_for_status()
        return _links_from_page(source, resp.text, str(resp.url))


def _links_from_page(source: str, body: str, page_url: str) -> list[tuple[str, str, str]]:
    soup = BeautifulSoup(body or "", "lxml")
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for tag in soup.find_all("a", href=True):
        href = _unwrap_redirect(urljoin(page_url, str(tag.get("href") or "")))
        cleaned = usable_url(href)
        if not cleaned or cleaned in seen or not host_allowed(cleaned):
            continue
        hit_title = " ".join((tag.get_text() or "").split())
        if len(hit_title) < 8:
            continue
        low = cleaned.lower()
        if not any(
            marker in low
            for marker in ("/document/", "guid=", "regnum=", "webnpa", ".pdf", "/upload/docs/")
        ):
            continue
        seen.add(cleaned)
        out.append((cleaned, source, hit_title))
    for url in _urls_from_html(body):
        if url in seen:
            continue
        seen.add(url)
        out.append((url, source, ""))
    return out


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
