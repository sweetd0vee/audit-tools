"""Knowledge pipeline orchestrator: ingest → index → summarize.

Bodies live in knowledge_ingest / knowledge_index / knowledge_summarize /
knowledge_ask / knowledge_owui. This module keeps the SSE build loop and
re-exports the previous public names so callers and tests keep working.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime

from app.services.document_artifact import ElapsedTimer, sse_status
from app.services.knowledge_ask import ask
from app.services.knowledge_index import embed_index, rebuild_index
from app.services.knowledge_ingest import add_uploaded_file, ingest_library
from app.services.knowledge_owui import export_pack_files, openwebui_status, sync_openwebui
from app.services.knowledge_summarize import (
    FRAGMENTS_PER_ITEM,
    fragments_from_item,
    retrieve_item_fragments,
    summarize_item,
)
from app.storage import store

__all__ = [
    "FRAGMENTS_PER_ITEM",
    "add_uploaded_file",
    "ask",
    "build_knowledge_events",
    "embed_index",
    "export_pack_files",
    "fragments_from_item",
    "ingest_library",
    "openwebui_status",
    "rebuild_index",
    "retrieve_item_fragments",
    "summarize_item",
    "sync_openwebui",
]


async def build_knowledge_events(case_id: str) -> AsyncIterator[dict]:
    timer = ElapsedTimer()
    elapsed = timer.ms

    yield sse_status(elapsed(), "Извлечение текста из файлов…")
    state = ingest_library(case_id)
    yield sse_status(elapsed(), "Нарезка на чанки…")
    rebuild_index(case_id, state=state)
    state = store.get(case_id)

    ok = sum(1 for i in state.knowledge if i.extract_status == "ok")
    yield sse_status(elapsed(), f"Карточки существенного: {ok} документов…")
    try:
        kws = list(state.keywords) + list(state.topics) + [state.inspection_name]
        yield sse_status(elapsed(), "Эмбеддинги для RAG-саммари…")
        await embed_index(case_id, kws)
    except Exception as exc:  # noqa: BLE001
        yield sse_status(elapsed(), f"Эмбеддинги пропущены ({exc}). Саммари пойдёт на BM25.")

    for item in state.knowledge:
        if item.extract_status != "ok":
            continue
        yield sse_status(elapsed(), f"Карточка существенного: {item.title}")
        await summarize_item(state, item)
        store.save(state)
        yield {
            "type": "summary",
            "item_id": item.id,
            "title": item.title,
            "status": item.summary_status,
            "summary": item.summary,
            "elapsed_ms": elapsed(),
        }

    state = store.get(case_id)
    state.meta["knowledge_built_at"] = datetime.utcnow().isoformat()
    store.save(state)
    yield {
        "type": "saved",
        "case_id": case_id,
        "knowledge": [k.model_dump() for k in state.knowledge],
        "elapsed_ms": elapsed(),
    }
