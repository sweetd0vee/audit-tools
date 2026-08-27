from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.config import settings
from app.domains import host_allowed
from app.filenames import slugify
from app.services.http_constants import DOWNLOAD_BROWSER_HEADERS, NEWS_MARKERS

logger = logging.getLogger(__name__)

_PLACEHOLDER = re.compile(r"(?:\.{3}|…)")


def usable_url(url: str | None) -> str | None:
    """Allowlist URL or None. Drops placeholders like https://pravo.by/..."""
    if not url or not isinstance(url, str):
        return None
    cleaned = url.strip().strip("`\"'<>").rstrip(").,;]")
    if cleaned.endswith("%60"):
        cleaned = cleaned[:-3]
    if not cleaned.lower().startswith(("http://", "https://")):
        return None
    if _PLACEHOLDER.search(cleaned):
        return None
    parsed = urlparse(cleaned)
    if not host_allowed(cleaned):
        return None
    if not (parsed.path or "").rstrip("/") and not parsed.query:
        return None
    return cleaned


def html_text(content: bytes) -> str:
    soup = BeautifulSoup(content, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)


def is_usable_npa_page(url: str, content: bytes, content_type: str) -> bool:
    """Reject 404 cards, news/analytics, and empty publication stubs."""
    low_url = (url or "").lower()
    low_type = (content_type or "").lower()
    if "404.php" in low_url:
        return False
    if "pdf" in low_type or low_url.endswith(".pdf"):
        return len(content or b"") > 400
    text = html_text(content or b"")
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) < 800:
        return False
    low_text = compact.lower()
    if "страница не найдена" in low_text or "page not found" in low_text:
        return False
    if any(marker in low_url for marker in NEWS_MARKERS):
        return False
    if "карточка документа" in low_text and "статья " not in low_text:
        if len(compact) < 4000:
            return False
    return True


def fulltext_links(content: bytes, page_url: str) -> list[str]:
    """PDF / guid=3871 / webnpa links from a publication card."""
    soup = BeautifulSoup(content or b"", "lxml")
    found: list[str] = []
    seen: set[str] = set()
    for tag in soup.find_all("a", href=True):
        href = urljoin(page_url, str(tag.get("href") or ""))
        cleaned = usable_url(href)
        if not cleaned or cleaned in seen:
            continue
        low = cleaned.lower()
        label = (tag.get_text() or "").lower()
        useful = (
            low.endswith(".pdf")
            or "/upload/docs/" in low
            or "guid=3871" in low
            or "webnpa/text" in low
            or "etalonline.by" in low and "/document/" in low
            or "скачать" in label
            or "текст" in label
        )
        if not useful:
            continue
        seen.add(cleaned)
        found.append(cleaned)
    return found


def _safe_filename(title: str, url: str, index: int) -> str:
    path = urlparse(url).path.lower()
    ext = ".pdf"
    if path.endswith(".html") or path.endswith(".htm") or path.endswith(".asp"):
        ext = ".html"
    elif path.endswith(".doc"):
        ext = ".doc"
    elif path.endswith(".docx"):
        ext = ".docx"
    elif path.endswith(".pdf"):
        ext = ".pdf"
    elif "html" in path:
        ext = ".html"
    return f"{index:02d}_{slugify(title)}{ext}"


async def _fetch(client: httpx.AsyncClient, url: str) -> tuple[str, bytes, str]:
    resp = await client.get(url)
    resp.raise_for_status()
    final_url = str(resp.url)
    if "404.php" in final_url.lower():
        raise ValueError(f"404 for {url}")
    content_type = (resp.headers.get("content-type") or "").lower()
    return final_url, resp.content, content_type


async def download_url(url: str, dest_dir: Path, title: str, index: int) -> dict:
    """Download URL into dest_dir. HTML saved as .html (+ optional .txt extract)."""
    url = usable_url(url) or url
    if not host_allowed(url):
        raise ValueError(f"Domain not allowed: {url}")

    dest_dir.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(
        timeout=settings.download_timeout_sec,
        follow_redirects=True,
        headers=DOWNLOAD_BROWSER_HEADERS,
    ) as client:
        final_url, content, content_type = await _fetch(client, url)
        if not is_usable_npa_page(final_url, content, content_type):
            followed = False
            for href in fulltext_links(content, final_url)[:4]:
                try:
                    hop_url, hop_content, hop_type = await _fetch(client, href)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("fulltext hop failed url=%s err=%s", href, exc)
                    continue
                if is_usable_npa_page(hop_url, hop_content, hop_type):
                    final_url, content, content_type = hop_url, hop_content, hop_type
                    followed = True
                    break
            if not followed:
                raise ValueError(f"No usable NPA text at {url}")

    filename = _safe_filename(title, final_url, index)
    if "pdf" in content_type and not filename.endswith(".pdf"):
        filename = filename.rsplit(".", 1)[0] + ".pdf"
    elif "html" in content_type and not filename.endswith(".html"):
        filename = filename.rsplit(".", 1)[0] + ".html"

    path = dest_dir / filename
    path.write_bytes(content)

    sha = hashlib.sha256(content).hexdigest()
    text_extract = None

    if filename.endswith(".html") or "html" in content_type:
        try:
            text = html_text(content)
            text_path = path.with_suffix(".txt")
            text_path.write_text(text, encoding="utf-8")
            text_extract = str(text_path)
        except Exception:
            text_extract = None

    return {
        "local_path": str(path),
        "text_extract": text_extract,
        "sha256": sha,
        "bytes": len(content),
        "content_type": content_type,
        "url": final_url,
    }
