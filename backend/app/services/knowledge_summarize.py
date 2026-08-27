from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.config import settings
from app.filenames import safe_stem
from app.models import CaseState, KnowledgeItem
from app.prompts import prompt
from app.services.chunker import (
    chunk_text,
    even_sample,
    keyword_score,
    sequential_windows,
)
from app.services.citations import (
    excerpt_for_cite,
    extract_article_outline,
    extract_article_ref,
    origin_url,
)
from app.services.knowledge_index import hydrate_item_chunks, persist_item_embeddings
from app.services.knowledge_ingest import item_text, summaries_dir
from app.services.knowledge_retrieve import retrieval_queries, select_evidence
from app.services.ollama_client import chat_complete, embed_texts

FRAGMENTS_PER_ITEM = 24
Progress = Callable[[str], Awaitable[None]]


def _format_outline(text: str, limit: int = 160) -> str:
    outline = extract_article_outline(text)
    if not outline:
        return "в тексте нет явных заголовков статей/глав — конспектируй по абзацам по порядку"
    extra = ""
    shown = outline
    if limit and len(outline) > limit:
        shown = outline[:limit]
        extra = f"\n… и ещё {len(outline) - limit} заголовков (они есть в оглавлении акта)"
    return "\n".join(f"- {line}" for line in shown) + extra


def _inspection_line(state: CaseState) -> tuple[str, str]:
    inspection = (state.inspection_name or "").strip() or "проверка"
    keywords = ", ".join(state.keywords) if state.keywords else "не указаны"
    return inspection, keywords


def fragments_from_spans(
    state: CaseState,
    item: KnowledgeItem,
    spans: list[str],
    start_n: int = 1,
) -> list[dict]:
    url = origin_url(state, item)
    out = []
    n = start_n
    for part in spans:
        out.append(
            {
                "n": n,
                "item_id": item.id,
                "title": item.title,
                "filename": item.filename,
                "article": extract_article_ref(part),
                "excerpt": excerpt_for_cite(part),
                "text": part,
                "url": url,
            }
        )
        n += 1
    return out


def fragments_from_item(
    state: CaseState,
    item: KnowledgeItem,
    start_n: int = 1,
    max_fragments: int | None = None,
) -> list[dict]:
    """Even sample of the whole act — fallback when RAG has not run yet."""
    text = item_text(item)
    if not text.strip():
        return []
    windows = sequential_windows(text)
    cap = max_fragments or FRAGMENTS_PER_ITEM
    picked = windows if len(windows) <= cap else even_sample(windows, cap)
    return fragments_from_spans(state, item, picked, start_n=start_n)


def _format_rag_body(frags: list[dict]) -> str:
    blocks = []
    for fr in frags:
        article = fr.get("article") or "фрагмент"
        blocks.append(f"[{fr['n']}] {article}\n{(fr.get('text') or '').strip()}")
    return "\n\n".join(blocks)


async def _chat_summary(user: str) -> str:
    return await chat_complete(
        prompt("summary_system"),
        user,
        temperature=0.1,
        timeout=settings.summary_timeout_sec,
        num_ctx=settings.ollama_num_ctx,
        num_predict=8192,
    )


async def retrieve_item_fragments(
    state: CaseState,
    item: KnowledgeItem,
    text: str,
    *,
    start_n: int = 1,
    on_status: Progress | None = None,
) -> list[dict]:
    """Query-focused RAG spans for this act (hybrid + RRF + MMR + neighbors)."""
    parts = chunk_text(text)
    if not parts:
        return []
    chunks = hydrate_item_chunks(state.case_id, item, parts)
    queries = retrieval_queries(state, extra=[item.title])
    if on_status:
        preview = ", ".join(queries[:4])
        await on_status(
            f"RAG по «{item.title}»: {len(parts)} чанков, запросы: {preview}"
        )
    evidence = await select_evidence(chunks, queries, embed_fn=embed_texts)
    persist_item_embeddings(state.case_id, chunks)
    spans = [(ev.get("text") or "").strip() for ev in evidence if (ev.get("text") or "").strip()]
    if not spans:
        ranked = sorted(parts, key=lambda p: keyword_score(p, queries), reverse=True)
        spans = ranked[:8] or parts[:4]
    return fragments_from_spans(state, item, spans, start_n=start_n)


async def _summarize_full_document(
    state: CaseState,
    item: KnowledgeItem,
    text: str,
    fragments: list[dict],
    on_status: Progress | None = None,
) -> str:
    inspection, keywords = _inspection_line(state)
    windows = sequential_windows(text)
    if len(windows) <= 1:
        if on_status:
            await on_status(f"Пишу карточку существенного: {item.title}")
        return await _chat_summary(
            prompt(
                "oneshot_card",
                inspection=inspection,
                keywords=keywords,
                title=item.title,
                source=item.source,
                outline=_format_outline(text, limit=80),
                body=text,
            )
        )

    if on_status:
        await on_status(
            f"Пишу карточку по RAG-выборке: {item.title} ({len(fragments)} фрагментов)"
        )
    card = await _chat_summary(
        prompt(
            "rag_card",
            inspection=inspection,
            keywords=keywords,
            title=item.title,
            source=item.source,
            chars=len(text),
            outline=_format_outline(text, limit=80),
            body=_format_rag_body(fragments),
        )
    )
    card = (card or "").strip()
    if card and "## Ключевые нормы" not in card and "## Основные положения" not in card:
        card = "## Ключевые нормы\n" + card
    return card


async def summarize_item(
    state: CaseState,
    item: KnowledgeItem,
    fragments: list[dict] | None = None,
    on_status: Progress | None = None,
) -> KnowledgeItem:
    """Карточка существенного: короткий акт целиком, длинный — query-focused RAG.
    `fragments` — отобранные фрагменты (и цитаты в приложение Word).
    """
    text = item_text(item)
    if not text.strip():
        item.summary_status = "failed"
        item.summary_error = "Нет текста для саммари"
        return item

    item.summary_status = "running"
    try:
        windows = sequential_windows(text)
        if fragments is None:
            if len(windows) <= 1:
                fragments = fragments_from_item(state, item)
            else:
                fragments = await retrieve_item_fragments(
                    state, item, text, on_status=on_status
                )
        item.citations = [
            {
                "n": fr["n"],
                "article": fr.get("article"),
                "excerpt": fr.get("excerpt"),
                "url": fr.get("url"),
                "title": fr.get("title") or item.title,
                "filename": fr.get("filename") or item.filename,
                "item_id": item.id,
                "text": fr.get("text"),
            }
            for fr in fragments
        ]
        item.summary = await _summarize_full_document(
            state, item, text, fragments, on_status=on_status
        )
        if not (item.summary or "").strip():
            raise ValueError("модель вернула пустой конспект")
        item.summary_status = "ok"
        item.summary_error = None
        out = summaries_dir(state.case_id) / f"{safe_stem(item.filename)}.md"
        out.write_text(f"# {item.title}\n\n{item.summary}\n", encoding="utf-8")
        item.summary_path = str(out)
    except Exception as exc:  # noqa: BLE001
        item.summary_status = "failed"
        item.summary_error = str(exc)
    return item
