from __future__ import annotations

import json
from pathlib import Path

from app.clock import utc_now
from app.config import settings
from app.models import CaseState, KnowledgeItem
from app.services.chunker import chunk_text, keyword_score
from app.services.citations import extract_article_ref
from app.services.knowledge_ingest import ingest_library, item_text
from app.services.knowledge_retrieve import chunks_from_item
from app.services.ollama_client import embed_texts
from app.storage import atomic_write_text, store


def index_path(case_id: str) -> Path:
    return store.case_dir(case_id) / "knowledge_index.json"


def load_index(case_id: str) -> dict:
    path = index_path(case_id)
    if not path.exists():
        return {"chunks": [], "embed_model": settings.ollama_embed_model}
    return json.loads(path.read_text(encoding="utf-8"))


def save_index(case_id: str, payload: dict) -> None:
    atomic_write_text(index_path(case_id), json.dumps(payload, ensure_ascii=False))


def rebuild_index(case_id: str, state: CaseState | None = None) -> dict:
    state = state or ingest_library(case_id)
    chunks_out: list[dict] = []
    for item in state.knowledge:
        if item.extract_status != "ok":
            continue
        text = item_text(item)
        parts = chunk_text(text)
        item.chunk_count = len(parts)
        for i, part in enumerate(parts):
            chunks_out.append(
                {
                    "id": f"{item.id}:{i}",
                    "item_id": item.id,
                    "title": item.title,
                    "filename": item.filename,
                    "text": part,
                    "article": extract_article_ref(part),
                    "embedding": [],
                }
            )
    payload = {
        "embed_model": settings.ollama_embed_model,
        "built_at": utc_now().isoformat(),
        "chunks": chunks_out,
    }
    save_index(case_id, payload)
    store.save(state)
    return payload


async def embed_index(case_id: str, keywords: list[str]) -> dict:
    """Embed a relevant subset of chunks (not the whole Civil Code)."""
    index = load_index(case_id)
    chunks = index.get("chunks") or []
    if not chunks:
        return index

    by_item: dict[str, list[dict]] = {}
    for ch in chunks:
        by_item.setdefault(ch["item_id"], []).append(ch)

    to_embed: list[dict] = []
    seen: set[str] = set()
    for group in by_item.values():
        scored = sorted(group, key=lambda c: keyword_score(c["text"], keywords), reverse=True)
        picked = scored[:60]
        if group and group[0]["id"] not in {c["id"] for c in picked}:
            picked.append(group[0])
        for ch in picked:
            if ch["id"] not in seen:
                seen.add(ch["id"])
                to_embed.append(ch)

    batch_size = 8
    for i in range(0, len(to_embed), batch_size):
        batch = to_embed[i : i + batch_size]
        texts = [c["text"][:4000] for c in batch]
        try:
            vectors = await embed_texts(texts)
        except Exception:
            break
        for ch, vec in zip(batch, vectors):
            ch["embedding"] = vec

    lookup = {c["id"]: c for c in to_embed}
    index["chunks"] = [lookup.get(c["id"], c) for c in chunks]
    index["embedded"] = sum(1 for c in index["chunks"] if c.get("embedding"))
    save_index(case_id, index)
    return index


def hydrate_item_chunks(case_id: str, item: KnowledgeItem, parts: list[str]) -> list[dict]:
    chunks = chunks_from_item(item, parts)
    index = load_index(case_id)
    by_id = {c.get("id"): c for c in index.get("chunks") or []}
    for ch in chunks:
        stored = by_id.get(ch["id"])
        if stored and stored.get("embedding"):
            ch["embedding"] = stored["embedding"]
    return chunks


def persist_item_embeddings(case_id: str, chunks: list[dict]) -> None:
    index = load_index(case_id)
    stored = index.get("chunks") or []
    if not stored:
        return
    lookup = {c["id"]: c for c in chunks if c.get("id") and c.get("embedding")}
    if not lookup:
        return
    changed = False
    for ch in stored:
        fresh = lookup.get(ch.get("id"))
        if fresh and fresh.get("embedding") and not ch.get("embedding"):
            ch["embedding"] = fresh["embedding"]
            changed = True
    if changed:
        index["embedded"] = sum(1 for c in stored if c.get("embedding"))
        save_index(case_id, index)


def drop_stale_embeddings(index: dict) -> bool:
    """Do not cosine-compare a qwen query vector with leftover MiniLM rows."""
    stored = (index.get("embed_model") or "").strip()
    current = (settings.ollama_embed_model or "").strip()
    if not stored or stored == current:
        return False
    for ch in index.get("chunks") or []:
        ch["embedding"] = []
    index["embed_model"] = current
    index["embedded"] = 0
    return True
