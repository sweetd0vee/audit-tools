from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from app.config import settings

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
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    if not any(host == d or host.endswith("." + d) for d in settings.domain_allowlist):
        return None
    if not (parsed.path or "").rstrip("/") and not parsed.query:
        return None
    return cleaned


def _safe_filename(title: str, url: str, index: int) -> str:
    base = re.sub(r"[^\w\u0400-\u04FF\-]+", "_", title, flags=re.UNICODE).strip("_")
    base = base[:80] or "document"
    ext = ".pdf"
    path = urlparse(url).path.lower()
    if path.endswith(".html") or path.endswith(".htm"):
        ext = ".html"
    elif path.endswith(".doc"):
        ext = ".doc"
    elif path.endswith(".docx"):
        ext = ".docx"
    elif path.endswith(".pdf"):
        ext = ".pdf"
    elif "html" in (urlparse(url).path.lower()):
        ext = ".html"
    return f"{index:02d}_{base}{ext}"


def _host_allowed(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in settings.domain_allowlist)


async def download_url(url: str, dest_dir: Path, title: str, index: int) -> dict:
    """Download URL into dest_dir. HTML saved as .html (+ optional .txt extract)."""
    url = usable_url(url) or url
    if not _host_allowed(url):
        raise ValueError(f"Domain not allowed: {url}")

    dest_dir.mkdir(parents=True, exist_ok=True)
    headers = {
        "User-Agent": "AuditToolsBot/1.0 (+local; bank-audit-research)",
        "Accept": "application/pdf,text/html,application/xhtml+xml,*/*",
    }

    async with httpx.AsyncClient(
        timeout=settings.download_timeout_sec,
        follow_redirects=True,
        headers=headers,
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        content = resp.content
        content_type = (resp.headers.get("content-type") or "").lower()

    # Decide extension
    filename = _safe_filename(title, url, index)
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
            soup = BeautifulSoup(content, "lxml")
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            text = soup.get_text("\n", strip=True)
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
        "url": url,
    }
