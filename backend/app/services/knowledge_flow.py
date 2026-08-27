from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from pathlib import Path

from app.config import settings
from app.filenames import safe_stem
from app.models import CaseState, KnowledgeItem
from app.prompts import prompt
from app.services.chunker import (
    chunk_text,
    even_sample,
    keyword_score,
    sequential_windows,
    tokenize,
)
from app.services.knowledge_retrieve import (
    chunks_from_item,
    retrieval_queries,
    retrieve_for_ask,
    select_evidence,
)
from app.services.citations import (
    excerpt_for_cite,
    extract_article_outline,
    extract_article_ref,
    origin_url,
)
from app.services.document_artifact import ElapsedTimer
from app.services.extract import TEXT_EXTS, extract_text
from app.services.ollama_client import chat_complete, embed_texts, rerank_texts
from app.services.openwebui_client import (
    OpenWebUIError,
    add_file_to_knowledge,
    ensure_collection,
    ping,
    upload_file,
)
from app.storage import store


def _index_path(case_id: str) -> Path:
    return store.case_dir(case_id) / "knowledge_index.json"


def _text_dir(case_id: str) -> Path:
    path = store.case_dir(case_id) / "knowledge_text"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _summaries_dir(case_id: str) -> Path:
    path = store.case_dir(case_id) / "summaries"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _is_duplicate_html(path: Path) -> bool:
    if path.suffix.lower() not in {".html", ".htm"}:
        return False
    return path.with_suffix(".txt").exists()


def _title_for_file(path: Path, state: CaseState) -> str:
    for doc in state.documents:
        if doc.local_path and Path(doc.local_path).name == path.name:
            return doc.title
        txt = Path(doc.local_path).with_suffix(".txt").name if doc.local_path else ""
        if txt and txt == path.name:
            return doc.title
    return path.stem.replace("_", " ")


def _origin_id(path: Path, state: CaseState) -> str | None:
    for doc in state.documents:
        if doc.local_path and Path(doc.local_path).name == path.name:
            return doc.id
    return None


def _load_index(case_id: str) -> dict:
    path = _index_path(case_id)
    if not path.exists():
        return {"chunks": [], "embed_model": settings.ollama_embed_model}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_index(case_id: str, payload: dict) -> None:
    _index_path(case_id).write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def ingest_library(case_id: str) -> CaseState:
    """Register files from knowledge_raw into knowledge items + extract text."""
    state = store.get(case_id)
    lib = store.library_dir(case_id)
    existing = {item.filename: item for item in state.knowledge}
    items = list(state.knowledge)
    text_dir = _text_dir(case_id)

    for path in sorted(lib.iterdir()):
        if not path.is_file() or path.suffix.lower() not in TEXT_EXTS:
            continue
        if _is_duplicate_html(path):
            continue
        if path.name in existing:
            continue
        try:
            text = extract_text(path)
        except Exception as exc:  # noqa: BLE001
            items.append(
                KnowledgeItem(
                    title=_title_for_file(path, state),
                    source="downloaded",
                    filename=path.name,
                    local_path=str(path),
                    origin_document_id=_origin_id(path, state),
                    bytes=path.stat().st_size,
                    extract_status="failed",
                    extract_error=str(exc),
                )
            )
            continue

        text_path = text_dir / f"{safe_stem(path.name)}.txt"
        text_path.write_text(text, encoding="utf-8")
        items.append(
            KnowledgeItem(
                title=_title_for_file(path, state),
                source="downloaded",
                filename=path.name,
                local_path=str(path),
                text_path=str(text_path),
                origin_document_id=_origin_id(path, state),
                bytes=path.stat().st_size,
                extract_status="ok",
                char_count=len(text),
            )
        )

    state.knowledge = items
    store.save(state)
    return state


def add_uploaded_file(case_id: str, filename: str, content: bytes) -> KnowledgeItem:
    state = store.get(case_id)
    lib = store.library_dir(case_id)
    safe = f"U_{len(state.knowledge)+1:02d}_{safe_stem(filename)}{Path(filename).suffix.lower() or '.bin'}"
    dest = lib / safe
    dest.write_bytes(content)

    item = KnowledgeItem(
        title=Path(filename).stem.replace("_", " "),
        source="uploaded",
        filename=dest.name,
        local_path=str(dest),
        bytes=len(content),
        extract_status="pending",
    )
    try:
        text = extract_text(dest)
        text_path = _text_dir(case_id) / f"{Path(safe).stem}.txt"
        text_path.write_text(text, encoding="utf-8")
        item.text_path = str(text_path)
        item.extract_status = "ok"
        item.char_count = len(text)
    except Exception as exc:  # noqa: BLE001
        item.extract_status = "failed"
        item.extract_error = str(exc)

    state.knowledge.append(item)
    store.write_library_archive(case_id)
    store.save(state)
    return item


def _item_text(item: KnowledgeItem) -> str:
    if item.text_path and Path(item.text_path).exists():
        return Path(item.text_path).read_text(encoding="utf-8")
    if item.local_path and Path(item.local_path).exists():
        return extract_text(Path(item.local_path))
    return ""


def rebuild_index(case_id: str, state: CaseState | None = None) -> dict:
    state = state or ingest_library(case_id)
    chunks_out: list[dict] = []
    for item in state.knowledge:
        if item.extract_status != "ok":
            continue
        text = _item_text(item)
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
                    "embedding": [],
                }
            )
    payload = {
        "embed_model": settings.ollama_embed_model,
        "built_at": datetime.utcnow().isoformat(),
        "chunks": chunks_out,
    }
    _save_index(case_id, payload)
    store.save(state)
    return payload


async def embed_index(case_id: str, keywords: list[str]) -> dict:
    """Embed a relevant subset of chunks (not the whole Civil Code)."""
    index = _load_index(case_id)
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
    _save_index(case_id, index)
    return index


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


def _fragments_from_spans(
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
    text = _item_text(item)
    if not text.strip():
        return []
    windows = sequential_windows(text)
    cap = max_fragments or FRAGMENTS_PER_ITEM
    picked = windows if len(windows) <= cap else even_sample(windows, cap)
    return _fragments_from_spans(state, item, picked, start_n=start_n)


_fragments_from_item = fragments_from_item


def _hydrate_item_chunks(case_id: str, item: KnowledgeItem, parts: list[str]) -> list[dict]:
    chunks = chunks_from_item(item, "", parts)
    index = _load_index(case_id)
    by_id = {c.get("id"): c for c in index.get("chunks") or []}
    for ch in chunks:
        stored = by_id.get(ch["id"])
        if stored and stored.get("embedding"):
            ch["embedding"] = stored["embedding"]
    return chunks


def _persist_item_embeddings(case_id: str, chunks: list[dict]) -> None:
    index = _load_index(case_id)
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
        _save_index(case_id, index)


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
    chunks = _hydrate_item_chunks(state.case_id, item, parts)
    queries = retrieval_queries(state, extra=[item.title])
    if on_status:
        preview = ", ".join(queries[:4])
        await on_status(
            f"RAG по «{item.title}»: {len(parts)} чанков, запросы: {preview}"
        )
    evidence = await select_evidence(chunks, queries, embed_fn=embed_texts)
    _persist_item_embeddings(state.case_id, chunks)
    spans = [(ev.get("text") or "").strip() for ev in evidence if (ev.get("text") or "").strip()]
    if not spans:
        ranked = sorted(parts, key=lambda p: keyword_score(p, queries), reverse=True)
        spans = ranked[:8] or parts[:4]
    return _fragments_from_spans(state, item, spans, start_n=start_n)


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
    text = _item_text(item)
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
        out = _summaries_dir(state.case_id) / f"{safe_stem(item.filename)}.md"
        out.write_text(f"# {item.title}\n\n{item.summary}\n", encoding="utf-8")
        item.summary_path = str(out)
    except Exception as exc:  # noqa: BLE001
        item.summary_status = "failed"
        item.summary_error = str(exc)
    return item


async def build_knowledge_events(case_id: str) -> AsyncIterator[dict]:
    timer = ElapsedTimer()
    elapsed = timer.ms

    yield {"type": "status", "message": "Извлечение текста из файлов…", "elapsed_ms": elapsed()}
    state = ingest_library(case_id)
    yield {"type": "status", "message": "Нарезка на чанки…", "elapsed_ms": elapsed()}
    rebuild_index(case_id, state=state)
    state = store.get(case_id)

    yield {
        "type": "status",
        "message": f"Карточки существенного: {sum(1 for i in state.knowledge if i.extract_status == 'ok')} документов…",
        "elapsed_ms": elapsed(),
    }
    try:
        kws = list(state.keywords) + list(state.topics) + [state.inspection_name]
        yield {"type": "status", "message": "Эмбеддинги для RAG-саммари…", "elapsed_ms": elapsed()}
        await embed_index(case_id, kws)
    except Exception as exc:  # noqa: BLE001
        yield {
            "type": "status",
            "message": f"Эмбеддинги пропущены ({exc}). Саммари пойдёт на BM25.",
            "elapsed_ms": elapsed(),
        }

    for item in state.knowledge:
        if item.extract_status != "ok":
            continue
        yield {
            "type": "status",
            "message": f"Карточка существенного: {item.title}",
            "elapsed_ms": elapsed(),
        }
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


def _summary_context(state: CaseState, question: str, budget: int = 8000) -> str:
    q_tokens = list(tokenize(question)) + [question]
    scored: list[tuple[float, str, str]] = []
    for item in state.knowledge:
        body = (item.summary or "").strip()
        if not body:
            continue
        score = keyword_score(f"{item.title}\n{body}", q_tokens)
        scored.append((score, item.title, body))
    scored.sort(key=lambda x: x[0], reverse=True)
    blocks: list[str] = []
    used = 0
    per_cap = 5000
    for _score, title, body in scored:
        take = body if len(body) <= per_cap else body[: per_cap - 1] + "…"
        piece = f"### {title}\n{take}"
        if used + len(piece) > budget and blocks:
            break
        blocks.append(piece)
        used += len(piece)
    return "\n\n".join(blocks)


def _drop_stale_embeddings(index: dict) -> bool:
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


async def ask(case_id: str, question: str, top_k: int | None = None) -> dict:
    top_k = top_k or settings.rag_top_k
    state = store.get(case_id)
    index = _load_index(case_id)
    chunks = index.get("chunks") or []
    if not chunks:
        rebuild_index(case_id)
        index = _load_index(case_id)
        chunks = index.get("chunks") or []
    if not chunks:
        raise ValueError("База знаний пуста. Сначала утвердите акты и дождитесь скачивания.")

    if _drop_stale_embeddings(index):
        chunks = index.get("chunks") or []
        _save_index(case_id, index)

    evidence = await retrieve_for_ask(
        chunks, question, top_k=top_k, embed_fn=embed_texts, rerank_fn=rerank_texts
    )
    _persist_item_embeddings(case_id, chunks)
    if not evidence:
        evidence = chunks[:top_k]

    context_parts = []
    sources = []
    used_embeddings = False
    used_reranker = False
    for i, ch in enumerate(evidence, start=1):
        article = extract_article_ref(ch.get("text") or "")
        title = ch.get("title") or ""
        label = f"{title} — {article}" if article else title
        context_parts.append(f"[{i}] {label}\n{ch['text']}")
        sources.append(
            {
                "n": i,
                "title": title,
                "filename": ch.get("filename"),
                "article": article,
                "excerpt": excerpt_for_cite(ch.get("text") or ""),
            }
        )
        if ch.get("embedding"):
            used_embeddings = True
        if ch.get("rerank_score") is not None:
            used_reranker = True
    summaries = _summary_context(state, question)
    summary_block = (
        f"Конспекты актов (ориентир, не источник номера статьи):\n{summaries}\n\n"
        if summaries
        else ""
    )
    user = prompt(
        "ask_user",
        inspection=state.inspection_name,
        keywords=", ".join(state.keywords),
        question=question,
        summary_block=summary_block,
        context="\n".join(context_parts),
    )
    answer = await chat_complete(
        prompt("ask_system"),
        user,
        temperature=0.1,
        timeout=settings.ollama_timeout_sec,
        num_ctx=settings.ollama_num_ctx,
    )
    return {
        "answer": answer,
        "sources": sources,
        "model": settings.ollama_model,
        "used_embeddings": used_embeddings,
        "used_reranker": used_reranker,
        "used_summaries": bool(summaries),
    }


def export_pack_files(case_id: str) -> list[tuple[str, bytes]]:
    """Files for Open WebUI / zip pack: clean texts + summaries + howto."""
    state = store.get(case_id)
    files: list[tuple[str, bytes]] = []
    for item in state.knowledge:
        if item.text_path and Path(item.text_path).exists():
            name = f"docs/{safe_stem(item.filename)}.txt"
            files.append((name, Path(item.text_path).read_bytes()))
        if item.summary:
            name = f"summaries/{safe_stem(item.filename)}.md"
            body = f"# {item.title}\n\n{item.summary}\n"
            files.append((name, body.encode("utf-8")))

    howto = f"""# Как подключить базу НПА в Open WebUI

Коллекция: {state.inspection_name}
Кейс: {case_id}

1. Откройте http://localhost:3000
2. Workspace → Knowledge → New Knowledge
3. Имя: «{state.inspection_name}»
4. Загрузите файлы из папки docs/ (чистый текст НПА)
5. По желанию добавьте summaries/
6. В чате с моделью нажмите # и выберите эту коллекцию
7. Задавайте вопросы по нормам. Модель получит фрагменты из базы + свои знания.

Системный промпт (вставьте в модель / чат):
{prompt("ask_system")}
"""
    files.append(("README_OPENWEBUI.md", howto.encode("utf-8")))
    return files


async def sync_openwebui(case_id: str, api_key: str | None = None) -> dict:
    key = (api_key or settings.openwebui_api_key or "").strip()
    if not key:
        raise OpenWebUIError("Нет API ключа Open WebUI. Укажите ключ или скачайте пакет вручную.")
    state = store.get(case_id)
    name = f"Аудит: {state.inspection_name}"[:80]
    desc = f"НПА кейса {case_id}. Ключевые слова: {', '.join(state.keywords)}"
    collection = await ensure_collection(name, desc, key)
    kid = collection.get("id") if isinstance(collection, dict) else collection
    if not kid:
        raise OpenWebUIError("Open WebUI не вернул id коллекции")
    kid = str(kid)

    uploaded = []
    text_dir = _text_dir(case_id)
    paths = sorted(text_dir.glob("*.txt"))
    if not paths:
        raise OpenWebUIError("Нет .txt для отправки. Сначала соберите базу знаний.")

    for path in paths:
        info = await upload_file(path, key)
        fid = info.get("id") if isinstance(info, dict) else None
        if not fid:
            uploaded.append({"file": path.name, "status": "error", "error": "нет file id"})
            continue
        try:
            await add_file_to_knowledge(kid, str(fid), key)
            uploaded.append({"file": path.name, "file_id": fid, "status": "ok"})
        except OpenWebUIError as exc:
            uploaded.append({"file": path.name, "file_id": fid, "status": "error", "error": str(exc)})

    state.meta["openwebui_knowledge_id"] = kid
    state.meta["openwebui_knowledge_name"] = name
    store.save(state)
    return {
        "knowledge_id": kid,
        "name": name,
        "uploaded": uploaded,
        "url": f"{settings.openwebui_url.rstrip('/')}/workspace/knowledge",
    }


async def openwebui_status(api_key: str | None = None) -> dict:
    try:
        return await ping(api_key)
    except Exception as exc:  # noqa: BLE001
        return {
            "url": settings.openwebui_url,
            "reachable": False,
            "auth": False,
            "error": str(exc),
        }
