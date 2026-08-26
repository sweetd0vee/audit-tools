"""Query-focused evidence selection for summarization and ask.

Pipeline (legal RAG, no extra services):
  multi-query → BM25 + keyword + dense → RRF → MMR → neighbor expand → merge.

This is retrieve-then-stuff (LangChain "stuff" + quote-style compression):
one LLM call over the selected spans, not map-reduce over the whole act.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence

from app.config import settings
from app.models import CaseState, KnowledgeItem
from app.services.chunker import (
    bm25_scores,
    cosine,
    jaccard,
    mmr_select,
    rrf_fuse,
    tokenize,
)

EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]


def retrieval_queries(state: CaseState, extra: Sequence[str] | None = None) -> list[str]:
    """Inspection + keywords as parallel queries (no extra LLM round-trip)."""
    out: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        text = " ".join((raw or "").split())
        key = text.lower()
        if len(text) < 3 or key in seen:
            return
        seen.add(key)
        out.append(text)

    _add(state.inspection_name or "")
    keywords = [k.strip() for k in state.keywords if (k or "").strip()]
    if keywords:
        _add(" ".join(keywords))
        for kw in keywords[:8]:
            _add(kw)
    for topic in list(state.topics or [])[:4]:
        _add(str(topic))
    for item in extra or []:
        _add(str(item))
    return out or [state.inspection_name or "проверка"]


def _chunk_id(ch: dict, idx: int) -> str:
    return str(ch.get("id") or f"{ch.get('item_id') or 'x'}:{idx}")


def _has_embedding(ch: dict) -> bool:
    emb = ch.get("embedding") or []
    return bool(emb) and any(float(x) != 0.0 for x in emb)


def _rank_ids(pairs: list[tuple[float, str]]) -> list[str]:
    ordered = sorted(pairs, key=lambda x: x[0], reverse=True)
    return [cid for score, cid in ordered if score > 0] or [cid for _score, cid in ordered]


def _minmax(values: list[float]) -> list[float]:
    if not values:
        return []
    lo = min(values)
    hi = max(values)
    if hi - lo < 1e-12:
        return [0.0 if v <= 0 else 1.0 for v in values]
    return [(v - lo) / (hi - lo) for v in values]


def _parse_index(cid: str) -> tuple[str, int] | None:
    if ":" not in cid:
        return None
    item_id, _, rest = cid.partition(":")
    try:
        return item_id, int(rest)
    except ValueError:
        return None


def _chunk_sim(left: dict, right: dict) -> float:
    if _has_embedding(left) and _has_embedding(right):
        return cosine(left["embedding"], right["embedding"])
    return jaccard(tokenize(left.get("text") or ""), tokenize(right.get("text") or ""))


def _expand_neighbors(lookup: dict[str, dict], picked: list[str], radius: int) -> list[str]:
    if radius <= 0:
        return list(picked)
    by_item: dict[str, dict[int, str]] = {}
    for cid, ch in lookup.items():
        parsed = _parse_index(cid)
        if parsed is None:
            continue
        item_id, idx = parsed
        by_item.setdefault(item_id, {})[idx] = cid
    out: list[str] = []
    seen: set[str] = set()
    for cid in picked:
        parsed = _parse_index(cid)
        if parsed is None:
            if cid not in seen:
                seen.add(cid)
                out.append(cid)
            continue
        item_id, idx = parsed
        group = by_item.get(item_id) or {}
        for j in range(idx - radius, idx + radius + 1):
            nid = group.get(j)
            if nid and nid not in seen:
                seen.add(nid)
                out.append(nid)
    return out


def _merge_consecutive(lookup: dict[str, dict], ids: list[str], budget: int) -> list[dict]:
    """Auto-merge neighboring hits so the model sees whole articles, not split spans."""
    ordered = sorted(
        ids,
        key=lambda cid: _parse_index(cid) or (cid, 0),
    )
    groups: list[list[str]] = []
    current: list[str] = []
    prev: tuple[str, int] | None = None
    for cid in ordered:
        parsed = _parse_index(cid)
        if (
            current
            and parsed
            and prev
            and parsed[0] == prev[0]
            and parsed[1] == prev[1] + 1
        ):
            current.append(cid)
        else:
            if current:
                groups.append(current)
            current = [cid]
        prev = parsed
    if current:
        groups.append(current)

    merged: list[dict] = []
    used = 0
    for group in groups:
        parts = [lookup[cid] for cid in group if cid in lookup]
        if not parts:
            continue
        text = "\n\n".join((p.get("text") or "").strip() for p in parts if (p.get("text") or "").strip())
        if not text:
            continue
        if used and used + len(text) > budget:
            break
        head = dict(parts[0])
        head["text"] = text
        head["id"] = group[0]
        head["merged_from"] = group
        merged.append(head)
        used += len(text)
        if used >= budget:
            break
    return merged


async def _embed_queries_and_chunks(
    queries: list[str],
    chunks: list[dict],
    embed_fn: EmbedFn | None,
    candidate_ids: set[str] | None = None,
) -> tuple[list[list[float]], int]:
    if embed_fn is None:
        return [], 0
    need: list[tuple[str, str]] = [("q", q) for q in queries]
    for ch in chunks:
        cid = _chunk_id(ch, 0)
        if candidate_ids is not None and cid not in candidate_ids:
            continue
        if not _has_embedding(ch):
            need.append((cid, (ch.get("text") or "")[:4000]))
    if not need:
        return [], 0

    vectors: list[list[float]] = []
    batch_size = 8
    for i in range(0, len(need), batch_size):
        batch = need[i : i + batch_size]
        texts = [t for _kind, t in batch]
        try:
            part = await embed_fn(texts)
        except Exception:
            return [], 0
        if len(part) != len(batch):
            return [], 0
        vectors.extend(part)

    q_embs: list[list[float]] = []
    by_id = {_chunk_id(ch, i): ch for i, ch in enumerate(chunks)}
    embedded = 0
    for (kind, _text), vec in zip(need, vectors):
        if kind == "q":
            q_embs.append(list(map(float, vec)))
            continue
        ch = by_id.get(kind)
        if ch is not None:
            ch["embedding"] = list(map(float, vec))
            embedded += 1
    return q_embs, embedded


async def select_evidence(
    chunks: list[dict],
    queries: list[str],
    *,
    top_k: int | None = None,
    budget_chars: int | None = None,
    candidates: int | None = None,
    neighbor: int | None = None,
    mmr_lambda: float | None = None,
    always_include_first: bool = True,
    embed_fn: EmbedFn | None = None,
) -> list[dict]:
    """Return document-ordered merged spans for one stuff-style LLM call."""
    if not chunks:
        return []
    top_k = top_k or settings.summary_rag_top_k
    budget = budget_chars or settings.summary_max_chars
    pool = candidates or settings.summary_rag_candidates
    radius = settings.summary_rag_neighbor if neighbor is None else neighbor
    lam = settings.summary_rag_mmr_lambda if mmr_lambda is None else mmr_lambda
    queries = [q for q in queries if (q or "").strip()] or ["проверка"]

    lookup: dict[str, dict] = {}
    tokenized: list[list[str]] = []
    ids: list[str] = []
    for i, ch in enumerate(chunks):
        cid = _chunk_id(ch, i)
        ch["id"] = cid
        lookup[cid] = ch
        ids.append(cid)
        tokenized.append(tokenize(ch.get("text") or ""))

    rankings: list[list[str]] = []
    for query in queries:
        q_tokens = tokenize(query)
        bm25 = bm25_scores(q_tokens, tokenized)
        rankings.append(_rank_ids([(s, cid) for s, cid in zip(bm25, ids)]))
        kw_scores = [_keyword_overlap(ch.get("text") or "", q_tokens) for ch in chunks]
        rankings.append(_rank_ids([(s, cid) for s, cid in zip(kw_scores, ids)]))

    # Candidate pool from lexical ranks before paying for embeddings.
    fused_lex = rrf_fuse(rankings)
    lex_ranked = sorted(fused_lex.items(), key=lambda x: x[1], reverse=True)
    candidate_ids = {cid for cid, _ in lex_ranked[:pool]}
    if always_include_first and ids:
        candidate_ids.add(ids[0])

    q_embs, _n = await _embed_queries_and_chunks(queries, chunks, embed_fn, candidate_ids)

    dense_rankings: list[list[str]] = []
    for q_emb in q_embs:
        pairs: list[tuple[float, str]] = []
        for cid in candidate_ids:
            ch = lookup[cid]
            if _has_embedding(ch):
                pairs.append((cosine(q_emb, ch["embedding"]), cid))
        if pairs:
            dense_rankings.append(_rank_ids(pairs))

    fused = rrf_fuse(rankings + dense_rankings) if dense_rankings else fused_lex
    if always_include_first and ids:
        fused[ids[0]] = fused.get(ids[0], 0.0) + 1.0 / 60.0

    cand_ids = [cid for cid, _ in sorted(fused.items(), key=lambda x: x[1], reverse=True) if cid in lookup]
    if pool:
        cand_ids = cand_ids[: max(pool, top_k)]
    if always_include_first and ids and ids[0] not in cand_ids:
        cand_ids = [ids[0]] + cand_ids

    rel_raw = [fused.get(cid, 0.0) for cid in cand_ids]
    rel_norm = _minmax(rel_raw)
    pairwise: dict[tuple[str, str], float] = {}
    for i, left_id in enumerate(cand_ids):
        for right_id in cand_ids[i + 1 :]:
            sim = _chunk_sim(lookup[left_id], lookup[right_id])
            pairwise[(left_id, right_id)] = sim
            pairwise[(right_id, left_id)] = sim

    picked = mmr_select(list(zip(rel_norm, cand_ids)), pairwise, top_k, lam)
    if always_include_first and ids and ids[0] not in picked:
        picked = [ids[0]] + picked[: max(0, top_k - 1)]

    expanded = _expand_neighbors(lookup, picked, radius)
    return _merge_consecutive(lookup, expanded, budget)


def _keyword_overlap(text: str, query_tokens: list[str]) -> float:
    blob = (text or "").lower()
    if not blob or not query_tokens:
        return 0.0
    score = 0.0
    for tok in query_tokens:
        if len(tok) < 3:
            continue
        score += blob.count(tok.lower())
    qset = {t for t in query_tokens if len(t) >= 3}
    cset = set(tokenize(text))
    if qset:
        score += 3.0 * len(qset & cset) / len(qset)
    return score


def chunks_from_item(item: KnowledgeItem, text: str, parts: list[str]) -> list[dict]:
    out: list[dict] = []
    for i, part in enumerate(parts):
        out.append(
            {
                "id": f"{item.id}:{i}",
                "item_id": item.id,
                "title": item.title,
                "filename": item.filename,
                "text": part,
                "embedding": [],
            }
        )
    return out
