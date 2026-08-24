from __future__ import annotations

import re

from app.models import CaseState, KnowledgeItem

ARTICLE_LINE_RE = re.compile(
    r"(?im)^\s*((?:статья|ст\.)\s+\d+(?:\.\d+)?(?:\s*[.\-–—:]\s*[^\n]{0,90})?)",
)
PUNKT_RE = re.compile(r"(?i)(?:пункт|п\.)\s+\d+(?:\.\d+)*")
CITE_RE = re.compile(r"\[(\d+)\]")


OUTLINE_RE = re.compile(
    r"(?im)^\s*((?:глава|раздел|часть|статья|ст\.)\s+[^\n]{1,120})",
)


def extract_article_outline(text: str, limit: int = 0) -> list[str]:
    """Ordered unique headings (статья/глава/раздел) from the full document."""
    found: list[str] = []
    seen: set[str] = set()
    for match in OUTLINE_RE.finditer(text or ""):
        title = re.sub(r"\s+", " ", match.group(1)).strip(" .")
        key = title.lower()
        if len(key) < 5 or key in seen:
            continue
        seen.add(key)
        found.append(title)
        if limit and len(found) >= limit:
            break
    return found


def extract_article_ref(text: str) -> str | None:
    blob = text or ""
    match = ARTICLE_LINE_RE.search(blob)
    if match:
        return re.sub(r"\s+", " ", match.group(1)).strip(" .")
    match = PUNKT_RE.search(blob)
    if match:
        return match.group(0)
    return None


def excerpt_for_cite(text: str, max_chars: int = 420) -> str:
    blob = re.sub(r"\s+", " ", (text or "")).strip()
    if len(blob) <= max_chars:
        return blob
    return blob[: max_chars - 1].rstrip() + "…"


def origin_url(state: CaseState, item: KnowledgeItem) -> str | None:
    for doc in state.documents:
        if item.origin_document_id and doc.id == item.origin_document_id:
            return doc.found_url or None
        if doc.title and item.title and doc.title.strip() == item.title.strip():
            return doc.found_url or None
        if doc.local_path and item.filename and doc.local_path.endswith(item.filename):
            return doc.found_url or None
    return None


def pages_estimate(text: str, chars_per_page: int = 1800) -> float:
    n = max(1, chars_per_page)
    return round(len(text or "") / n, 1)
