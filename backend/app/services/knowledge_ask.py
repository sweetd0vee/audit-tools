from __future__ import annotations

import logging
from datetime import datetime

from app.config import settings
from app.prompts import prompt
from app.services.citations import excerpt_for_cite, extract_article_ref, origin_url
from app.services.knowledge_index import (
    drop_stale_embeddings,
    load_index,
    persist_item_embeddings,
    rebuild_index,
    save_index,
)
from app.services.knowledge_retrieve import retrieve_for_ask
from app.services.ollama_client import chat_complete, embed_texts, rerank_texts
from app.storage import store

logger = logging.getLogger(__name__)


def _refuse_ask(case_id: str, question: str, reason: str) -> dict:
    answer = prompt("ask_refuse")
    payload = {
        "answer": answer.strip(),
        "sources": [],
        "model": settings.ollama_model,
        "used_embeddings": False,
        "used_reranker": False,
        "used_summaries": False,
        "refused": True,
        "refuse_reason": reason,
    }
    _append_ask_trail(case_id, question, payload, reason=reason)
    logger.info("ask refused case=%s reason=%s", case_id, reason)
    return payload


def _append_ask_trail(
    case_id: str,
    question: str,
    payload: dict,
    *,
    reason: str | None = None,
    evidence: list[dict] | None = None,
) -> None:
    sources = payload.get("sources") or []
    record = {
        "ts": datetime.utcnow().isoformat(),
        "case_id": case_id,
        "question": question,
        "refused": bool(payload.get("refused")),
        "reason": reason,
        "chunk_ids": [ch.get("id") for ch in (evidence or []) if ch.get("id")],
        "filenames": [s.get("filename") for s in sources if s.get("filename")],
        "articles": [s.get("article") for s in sources if s.get("article")],
        "scores": [
            {
                "id": ch.get("id"),
                "rerank": ch.get("rerank_score"),
                "fused": ch.get("fused_score"),
                "lexical": ch.get("lexical_score"),
            }
            for ch in (evidence or [])
        ],
        "used_embeddings": bool(payload.get("used_embeddings")),
        "used_reranker": bool(payload.get("used_reranker")),
    }
    try:
        store.append_jsonl(case_id, "trail/ask.jsonl", record)
    except Exception:
        logger.warning("ask trail write failed case=%s", case_id, exc_info=True)


async def ask(case_id: str, question: str, top_k: int | None = None) -> dict:
    top_k = top_k or settings.rag_top_k
    state = store.get(case_id)
    index = load_index(case_id)
    chunks = index.get("chunks") or []
    if not chunks:
        rebuild_index(case_id)
        index = load_index(case_id)
        chunks = index.get("chunks") or []
    if not chunks:
        raise ValueError("База знаний пуста. Сначала утвердите акты и дождитесь скачивания.")

    if drop_stale_embeddings(index):
        chunks = index.get("chunks") or []
        save_index(case_id, index)

    evidence = await retrieve_for_ask(
        chunks, question, top_k=top_k, embed_fn=embed_texts, rerank_fn=rerank_texts
    )
    persist_item_embeddings(case_id, chunks)
    if not evidence:
        return _refuse_ask(case_id, question, "no_evidence")

    by_item = {item.id: item for item in state.knowledge}
    context_parts = []
    sources = []
    used_embeddings = False
    used_reranker = False
    for i, ch in enumerate(evidence, start=1):
        article = ch.get("article") or extract_article_ref(ch.get("text") or "")
        title = ch.get("title") or ""
        label = f"{title} — {article}" if article else title
        context_parts.append(f"[{i}] {label}\n{ch['text']}")
        item = by_item.get(ch.get("item_id") or "")
        sources.append(
            {
                "n": i,
                "title": title,
                "filename": ch.get("filename"),
                "item_id": ch.get("item_id"),
                "chunk_id": ch.get("id"),
                "article": article,
                "excerpt": excerpt_for_cite(ch.get("text") or ""),
                "url": origin_url(state, item) if item else None,
                "score": ch.get("rerank_score", ch.get("fused_score")),
            }
        )
        if ch.get("embedding"):
            used_embeddings = True
        if ch.get("rerank_score") is not None:
            used_reranker = True
    user = prompt(
        "ask_user",
        inspection=state.inspection_name,
        keywords=", ".join(state.keywords),
        question=question,
        context="\n".join(context_parts),
    )
    answer = await chat_complete(
        prompt("ask_system"),
        user,
        temperature=0.1,
        timeout=settings.ollama_timeout_sec,
        num_ctx=settings.ollama_num_ctx,
    )
    payload = {
        "answer": answer,
        "sources": sources,
        "model": settings.ollama_model,
        "used_embeddings": used_embeddings,
        "used_reranker": used_reranker,
        "used_summaries": False,
        "refused": False,
        "refuse_reason": None,
    }
    _append_ask_trail(case_id, question, payload, evidence=evidence)
    logger.info(
        "ask case=%s sources=%s embeddings=%s rerank=%s",
        case_id,
        len(sources),
        used_embeddings,
        used_reranker,
    )
    return payload
