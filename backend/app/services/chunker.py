from __future__ import annotations

import math
import re

from app.config import settings

TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+", re.UNICODE)
ARTICLE_RE = re.compile(
    r"(?=^(?:Статья|Ст\.|Глава|ГЛАВА|СТАТЬЯ)\s+\d+)",
    re.MULTILINE,
)
SECTION_RE = re.compile(
    r"(?=^(?:Статья|Ст\.|Глава|ГЛАВА|СТАТЬЯ|Раздел|РАЗДЕЛ)\s+\d+)",
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


def even_sample(items: list, k: int) -> list:
    """Keep first, last and evenly spaced items so a long document is represented throughout."""
    if k <= 0:
        return []
    n = len(items)
    if n <= k:
        return list(items)
    if k == 1:
        return [items[0]]
    out = []
    seen: set[int] = set()
    for i in range(k):
        idx = int(round(i * (n - 1) / (k - 1)))
        if idx in seen:
            continue
        seen.add(idx)
        out.append(items[idx])
    return out


def split_sections(text: str) -> list[str]:
    """Keep article/chapter blocks intact so a window never starts mid-article."""
    text = (text or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in SECTION_RE.split(text) if p and p.strip()]
    return parts or [text]


def sequential_windows(
    text: str,
    size: int | None = None,
    overlap: int | None = None,
) -> list[str]:
    """Ordered windows covering the whole document.

    Packs whole articles/chapters until `size`. Never keyword-ranks, never
    skips the middle. Overlap is used only when a single article is longer
    than the window.
    """
    size = size or settings.summary_section_chars
    overlap = overlap if overlap is not None else settings.summary_section_overlap
    text = (text or "").strip()
    if not text:
        return []
    parts = split_sections(text)
    if len(parts) <= 1:
        return chunk_text(text, size=size, overlap=overlap)

    windows: list[str] = []
    buf = ""
    for part in parts:
        if len(part) > size:
            if buf:
                windows.append(buf.strip())
                buf = ""
            windows.extend(_window(part, size, overlap))
            continue
        if buf and len(buf) + 2 + len(part) > size:
            windows.append(buf.strip())
            buf = part
        else:
            buf = f"{buf}\n\n{part}" if buf else part
    if buf.strip():
        windows.append(buf.strip())
    return windows


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


def jaccard(a: list[str] | set[str], b: list[str] | set[str]) -> float:
    left = set(a)
    right = set(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def bm25_scores(
    query_tokens: list[str],
    docs: list[list[str]],
    *,
    k1: float = 1.5,
    b: float = 0.75,
) -> list[float]:
    """Okapi BM25 over already-tokenized documents. No extra dependencies."""
    n = len(docs)
    if n == 0:
        return []
    avgdl = sum(len(doc) for doc in docs) / n
    df: dict[str, int] = {}
    for doc in docs:
        for tok in set(doc):
            df[tok] = df.get(tok, 0) + 1
    query = [t for t in query_tokens if t]
    out: list[float] = []
    for doc in docs:
        tf: dict[str, int] = {}
        for tok in doc:
            tf[tok] = tf.get(tok, 0) + 1
        dl = len(doc) or 1
        score = 0.0
        for tok in query:
            n_qi = df.get(tok, 0)
            if n_qi <= 0:
                continue
            idf = math.log(1.0 + (n - n_qi + 0.5) / (n_qi + 0.5))
            freq = tf.get(tok, 0)
            denom = freq + k1 * (1.0 - b + b * dl / max(avgdl, 1e-9))
            score += idf * (freq * (k1 + 1.0)) / denom
        out.append(score)
    return out


def rrf_fuse(rankings: list[list[str]], k: int = 60) -> dict[str, float]:
    """Reciprocal Rank Fusion — scale-agnostic merge of BM25 and dense ranks."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        seen: set[str] = set()
        rank = 0
        for cid in ranking:
            if not cid or cid in seen:
                continue
            seen.add(cid)
            rank += 1
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
    return scores


def mmr_select(
    candidates: list[tuple[float, str]],
    pairwise: dict[tuple[str, str], float],
    top_k: int,
    lambda_mult: float = 0.7,
) -> list[str]:
    """Maximal Marginal Relevance: relevance minus redundancy (Carbonell & Goldstein)."""
    if top_k <= 0 or not candidates:
        return []
    remaining = list(candidates)
    selected: list[str] = []
    first = max(remaining, key=lambda x: x[0])
    selected.append(first[1])
    remaining = [c for c in remaining if c[1] != first[1]]
    while remaining and len(selected) < top_k:
        best_id = remaining[0][1]
        best_score = float("-inf")
        for rel, cid in remaining:
            redundancy = 0.0
            for other in selected:
                pair = pairwise.get((cid, other), pairwise.get((other, cid), 0.0))
                if pair > redundancy:
                    redundancy = pair
            score = lambda_mult * rel - (1.0 - lambda_mult) * redundancy
            if score > best_score:
                best_score = score
                best_id = cid
        selected.append(best_id)
        remaining = [c for c in remaining if c[1] != best_id]
    return selected
