from __future__ import annotations

import math
import re

from app.config import settings

TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+", re.UNICODE)
ARTICLE_RE = re.compile(
    r"(?=^(?:Статья|Ст\.|Глава|ГЛАВА|СТАТЬЯ)\s+\d+)",
    re.MULTILINE,
)


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in TOKEN_RE.findall(text or "")]


def chunk_text(text: str, size: int | None = None, overlap: int | None = None) -> list[str]:
    size = size or settings.chunk_size
    overlap = overlap or settings.chunk_overlap
    text = (text or "").strip()
    if not text:
        return []

    parts = [p.strip() for p in ARTICLE_RE.split(text) if p and p.strip()]
    if len(parts) <= 1:
        parts = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    if not parts:
        parts = [text]

    chunks: list[str] = []
    buf = ""
    for part in parts:
        if len(part) > size * 2:
            if buf:
                chunks.append(buf.strip())
                buf = ""
            chunks.extend(_window(part, size, overlap))
            continue
        if buf and len(buf) + 2 + len(part) > size:
            chunks.append(buf.strip())
            tail = buf[-overlap:] if overlap and len(buf) > overlap else ""
            buf = (tail + "\n\n" + part).strip()
        else:
            buf = (buf + "\n\n" + part).strip() if buf else part
    if buf.strip():
        chunks.append(buf.strip())
    return chunks


def _window(text: str, size: int, overlap: int) -> list[str]:
    if len(text) <= size:
        return [text]
    step = max(1, size - overlap)
    out = []
    i = 0
    while i < len(text):
        out.append(text[i : i + size].strip())
        i += step
    return [c for c in out if c]


def keyword_score(text: str, keywords: list[str]) -> float:
    if not text or not keywords:
        return 0.0
    blob = text.lower()
    score = 0.0
    for kw in keywords:
        k = (kw or "").strip().lower()
        if len(k) < 3:
            continue
        score += blob.count(k) * (1.0 + math.log(len(k)))
    return score


def pick_relevant_chunks(
    chunks: list[str],
    keywords: list[str],
    max_chars: int | None = None,
    always_include_first: int = 1,
) -> list[str]:
    max_chars = max_chars or settings.summary_max_chars
    if not chunks:
        return []
    ranked = sorted(
        enumerate(chunks),
        key=lambda iv: keyword_score(iv[1], keywords),
        reverse=True,
    )
    chosen: dict[int, str] = {}
    for i in range(min(always_include_first, len(chunks))):
        chosen[i] = chunks[i]
    for idx, ch in ranked:
        if idx in chosen:
            continue
        chosen[idx] = ch
        if sum(len(v) for v in chosen.values()) >= max_chars:
            break
    ordered = [chosen[i] for i in sorted(chosen)]
    total = 0
    out = []
    for ch in ordered:
        if total + len(ch) > max_chars and out:
            break
        out.append(ch)
        total += len(ch)
    return out


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))
