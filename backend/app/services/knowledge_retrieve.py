"""Query-focused evidence selection for summarization and ask.

Pipeline (legal RAG, no extra services):
  multi-query → BM25 + keyword + dense → RRF → rerank → MMR → neighbor expand → merge.

This is retrieve-then-stuff (LangChain "stuff" + quote-style compression):
one LLM call over the selected spans, not map-reduce over the whole act.
"""

from __future__ import annotations

import logging
import math
import re
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

logger = logging.getLogger(__name__)

EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]
RerankFn = Callable[[str, list[str]], Awaitable[list[float]]]

# «ст. 625», «статья 12.1», «ст 38» — BM25 must see the canonical heading, not just the digits.
ARTICLE_MENTION_RE = re.compile(
    r"(?i)(?:статьи|статью|статьёй|статьей|статье|статья|ст\.?)\s*№?\s*(\d+(?:\.\d+)?)"
)
ARTICLE_HEADING_RE = re.compile(
    r"(?im)^\s*(?:#{1,6}\s+)?(?:статья|ст\.)\s+(\d+(?:\.\d+)?)\b"
)
PUNKT_MENTION_RE = re.compile(
    r"(?i)(?:пункта|пункту|пункте|пунктом|пункт|п\.)\s*(\d+(?:\.\d+)*)"
)


def punkt_nums_in_query(question: str) -> set[str]:
    return {m.group(1) for m in PUNKT_MENTION_RE.finditer(question or "")}


def punkt_nums_in_text(text: str) -> set[str]:
    return {m.group(1) for m in PUNKT_MENTION_RE.finditer(text or "")}


def article_query_variants(question: str) -> list[str]:
    """Turn 'ст. 625 ГК' into heading-shaped queries so BM25 hits 'Статья 625'."""
    out: list[str] = []
    seen: set[str] = set()
    for match in ARTICLE_MENTION_RE.finditer(question or ""):
        num = match.group(1)
        if num in seen:
            continue
        seen.add(num)
        out.extend([f"Статья {num}", f"Ст. {num}", f"статья {num}", f"## Статья {num}"])
    for match in PUNKT_MENTION_RE.finditer(question or ""):
        num = match.group(1)
        key = f"p:{num}"
        if key in seen:
            continue
        seen.add(key)
        out.extend([f"пункт {num}", f"п. {num}"])
    return out


def ask_queries(question: str) -> list[str]:
    q = " ".join((question or "").split())
    out: list[str] = []
    seen: set[str] = set()
    for item in [q, *article_query_variants(q)]:
        key = item.lower()
        if len(item) < 3 or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out or [question or "проверка"]


def _article_num(text: str) -> str | None:
    match = ARTICLE_HEADING_RE.search(text or "")
    return match.group(1) if match else None


def _heading_boosts(queries: list[str], lookup: dict[str, dict]) -> dict[str, float]:
    """RRF is ~0.03/ranker; a heading match must outrank a cross-reference to the same number."""
    nums: list[str] = []
    seen: set[str] = set()
    for match in ARTICLE_MENTION_RE.finditer(" ".join(queries)):
        num = match.group(1)
        if num not in seen:
            seen.add(num)
            nums.append(num)
    if not nums:
        return {}
    boosts: dict[str, float] = {}
    for cid, ch in lookup.items():
        head_num = _article_num((ch.get("text") or "")[:240])
        if head_num and head_num in seen:
            boosts[cid] = 2.0
    return boosts


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
        origin_num = _article_num((lookup.get(cid) or {}).get("text") or "")
        for j in range(idx - radius, idx + radius + 1):
            nid = group.get(j)
            if not nid or nid in seen:
                continue
            other_num = _article_num((lookup.get(nid) or {}).get("text") or "")
            if origin_num and other_num and other_num != origin_num:
                continue
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
            head_num = _article_num((lookup.get(current[0]) or {}).get("text") or "")
            next_num = _article_num((lookup.get(cid) or {}).get("text") or "")
            if head_num and next_num and head_num != next_num:
                groups.append(current)
                current = [cid]
            else:
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
        scores = [p.get("rerank_score") for p in parts if p.get("rerank_score") is not None]
        if scores:
            head["rerank_score"] = max(float(s or 0) for s in scores)
        fused_scores = [p.get("fused_score") for p in parts if p.get("fused_score") is not None]
        if fused_scores:
            head["fused_score"] = max(float(s or 0) for s in fused_scores)
        lex_scores = [p.get("lexical_score") for p in parts if p.get("lexical_score") is not None]
        if lex_scores:
            head["lexical_score"] = max(float(s or 0) for s in lex_scores)
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
        found = by_id.get(kind)
        if found is not None:
            found["embedding"] = list(map(float, vec))
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
    rerank_fn: RerankFn | None = None,
    rerank_query: str | None = None,
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
    for cid, boost in _heading_boosts(queries, lookup).items():
        fused[cid] = fused.get(cid, 0.0) + boost
    for cid, boost in _title_boosts(queries, lookup).items():
        fused[cid] = fused.get(cid, 0.0) + boost
    if always_include_first and ids:
        fused[ids[0]] = fused.get(ids[0], 0.0) + 1.0 / 60.0
    for cid, score in fused.items():
        if cid in lookup:
            lookup[cid]["fused_score"] = float(score)

    cand_ids = [cid for cid, _ in sorted(fused.items(), key=lambda x: x[1], reverse=True) if cid in lookup]
    if pool:
        cand_ids = cand_ids[: max(pool, top_k)]
    if always_include_first and ids and ids[0] not in cand_ids:
        cand_ids = [ids[0]] + cand_ids

    rerank_scores = await _rerank_candidates(
        lookup, cand_ids, queries, rerank_fn, rerank_query
    )
    if rerank_scores:
        ranked = sorted(rerank_scores.items(), key=lambda x: x[1], reverse=True)
        reranked = [cid for cid, _ in ranked]
        rest = [cid for cid in cand_ids if cid not in rerank_scores]
        cand_ids = reranked + rest

    rel_raw = [
        rerank_scores.get(cid, fused.get(cid, 0.0)) if rerank_scores else fused.get(cid, 0.0)
        for cid in cand_ids
    ]
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


async def _rerank_candidates(
    lookup: dict[str, dict],
    cand_ids: list[str],
    queries: list[str],
    rerank_fn: RerankFn | None,
    rerank_query: str | None,
) -> dict[str, float]:
    if rerank_fn is None or not cand_ids:
        return {}
    limit = max(settings.rag_rerank_candidates, 1)
    pool_ids = cand_ids[:limit]
    query = (rerank_query or (queries[0] if queries else "") or "").strip()
    if not query:
        return {}
    docs = [(lookup[cid].get("text") or "")[:4000] for cid in pool_ids]
    try:
        scores = await rerank_fn(query, docs)
    except Exception:
        logger.warning("Rerank failed; keeping hybrid order", exc_info=True)
        return {}
    if not scores or len(scores) != len(pool_ids):
        return {}
    if max(scores) - min(scores) < 1e-9:
        return {}
    out: dict[str, float] = {}
    for cid, score in zip(pool_ids, scores):
        value = float(score)
        out[cid] = value
        lookup[cid]["rerank_score"] = value
    return out


def corpus_article_nums(chunks: list[dict]) -> set[str]:
    return {num for ch in chunks if (num := _article_num(ch.get("text") or ""))}


def _idf_lexical_score(question: str, text: str, df: dict[str, int], n_docs: int) -> float:
    """Count query terms that are rare in this library. Stops 'срок' from matching everything."""
    q_tokens = [t for t in tokenize(question) if len(t) >= 4]
    if not q_tokens or n_docs <= 0:
        return 0.0
    blob = set(tokenize(text))
    score = 0.0
    for tok in q_tokens:
        if tok not in blob:
            continue
        freq = df.get(tok, 0)
        if freq / n_docs >= 0.45:
            continue
        score += math.log(1.0 + n_docs / (freq + 0.5))
    return score


def _chunk_passes_gate(question: str, chunk: dict) -> bool:
    text = chunk.get("text") or ""
    mentioned = article_nums_in_query(question)
    heading = _article_num(text)
    if mentioned and heading and heading in mentioned:
        return True
    rerank = chunk.get("rerank_score")
    if rerank is not None:
        return float(rerank) >= settings.rag_min_rerank
    lexical = chunk.get("lexical_score")
    if lexical is None:
        lexical = _keyword_overlap(text, tokenize(question))
    return float(lexical) >= settings.rag_min_lexical


def _requested_title_keys(question: str) -> list[str]:
    q = f" {(question or '').lower()} "
    keys: list[str] = []
    if any(x in q for x in (" гражданск", " гк ", " гк.", " гк,")):
        keys.append("гражданск")
    if any(x in q for x in (" налогов", " нк ", " нк.", " нк,")):
        keys.append("налог")
    if any(x in q for x in (" нбрб", " национального банка")):
        keys.append("нбрб")
    return keys


def _title_matches(chunk: dict, keys: list[str]) -> bool:
    hay = f"{chunk.get('title') or ''} {chunk.get('filename') or ''}".lower()
    aliases = {
        "гражданск": ("гражданск", "гк"),
        "налог": ("налог", "нк"),
        "нбрб": ("нбрб", "национальн"),
    }
    for key in keys:
        if any(alias in hay for alias in aliases.get(key, (key,))):
            return True
    return False


def gate_ask_evidence(
    question: str,
    evidence: list[dict],
    *,
    corpus_articles: set[str] | None = None,
) -> list[dict]:
    """Drop weak / wrong-article spans. Empty result means the caller must refuse."""
    mentioned = article_nums_in_query(question)
    if mentioned and corpus_articles is not None and not (mentioned & corpus_articles):
        return []
    kept = [ch for ch in evidence if _chunk_passes_gate(question, ch)]
    if mentioned:
        heading_hits = [ch for ch in kept if _article_num(ch.get("text") or "") in mentioned]
        kept = heading_hits
        if not kept:
            return []
    title_keys = _requested_title_keys(question)
    if title_keys:
        titled = [ch for ch in kept if _title_matches(ch, title_keys)]
        if titled:
            return titled
        return []
    return kept


def _title_boosts(queries: list[str], lookup: dict[str, dict]) -> dict[str, float]:
    qset = {t for t in tokenize(" ".join(queries)) if len(t) >= 4}
    if not qset:
        return {}
    boosts: dict[str, float] = {}
    for cid, ch in lookup.items():
        tset = set(tokenize(ch.get("title") or ""))
        if qset & tset:
            boosts[cid] = 0.35
    return boosts


async def retrieve_for_ask(
    chunks: list[dict],
    question: str,
    *,
    top_k: int | None = None,
    embed_fn: EmbedFn | None = None,
    rerank_fn: RerankFn | None = None,
) -> list[dict]:
    """Library-wide hybrid retrieve for `вопрос …`. Empty list = refuse, never preamble."""
    inventory = corpus_article_nums(chunks)
    mentioned = article_nums_in_query(question)
    if mentioned and inventory and not (mentioned & inventory):
        return []
    evidence = await select_evidence(
        chunks,
        ask_queries(question),
        top_k=top_k or settings.rag_top_k,
        budget_chars=max(settings.summary_max_chars, 24000),
        candidates=settings.rag_candidates,
        neighbor=settings.rag_neighbor,
        mmr_lambda=settings.rag_mmr_lambda,
        always_include_first=False,
        embed_fn=embed_fn,
        rerank_fn=rerank_fn,
        rerank_query=question,
    )
    n = len(chunks) or 1
    df: dict[str, int] = {}
    for ch in chunks:
        for tok in set(tokenize(ch.get("text") or "")):
            df[tok] = df.get(tok, 0) + 1
    for span in evidence:
        span["lexical_score"] = _idf_lexical_score(
            question, span.get("text") or "", df, n
        )
    return gate_ask_evidence(question, evidence, corpus_articles=inventory)


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


def chunks_from_item(item: KnowledgeItem, parts: list[str]) -> list[dict]:
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
