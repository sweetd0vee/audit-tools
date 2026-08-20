from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

from app.config import settings
from app.models import CaseState, KnowledgeItem, new_id
from app.services.chunker import chunk_text, cosine, keyword_score, pick_relevant_chunks, tokenize
from app.services.extract import TEXT_EXTS, extract_text
from app.services.ollama_client import chat_complete, embed_texts
from app.services.openwebui_client import (
    OpenWebUIError,
    add_file_to_knowledge,
    ensure_collection,
    ping,
    upload_file,
)
from app.storage import store

SUMMARY_SYSTEM = """Ты — старший аудитор банка в Республике Беларусь.
Тебе дают фрагменты актуального НПА и тему проверки.
Напиши практическое саммари для аудитора, который НЕ будет читать весь акт.

Правила:
1. Только то, что есть во фрагментах. Не выдумывай статьи и пункты.
2. Указывай номера статей / пунктов, если они есть в тексте.
3. Фокус — тема проверки и ключевые слова, а не пересказ всего кодекса.
4. Если во фрагментах мало релевантного — честно напиши, каких глав/статей не хватает.
5. Пиши по-русски, кратко, структурировано.
"""

ASK_SYSTEM = """Ты — ассистент внутреннего аудитора банка РБ.
Отвечай на вопрос, опираясь В ПЕРВУЮ ОЧЕРЕДЬ на фрагменты НПА из базы знаний.
Правила:
1. Цитируй документ и, если есть, статью/пункт.
2. Если во фрагментах нет ответа — так и скажи. Можно добавить общую оговорку из своих знаний, явно пометив «не из базы НПА».
3. Не выдумывай номера норм.
"""


def _safe_stem(name: str) -> str:
    base = Path(name).stem
    base = re.sub(r"[^\w\u0400-\u04FF\-]+", "_", base, flags=re.UNICODE).strip("_")
    return (base[:80] or "document")


def _index_path(case_id: str) -> Path:
    return store._case_dir(case_id) / "knowledge_index.json"


def _text_dir(case_id: str) -> Path:
    path = store._case_dir(case_id) / "knowledge_text"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _summaries_dir(case_id: str) -> Path:
    path = store._case_dir(case_id) / "summaries"
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

        text_path = text_dir / f"{_safe_stem(path.name)}.txt"
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
    safe = f"U_{len(state.knowledge)+1:02d}_{_safe_stem(filename)}{Path(filename).suffix.lower() or '.bin'}"
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


def _keywords(state: CaseState, item: KnowledgeItem) -> list[str]:
    kws = list(state.keywords) + list(state.topics)
    kws.append(state.inspection_name)
    if item.title:
        kws.extend(item.title.split())
    for doc in state.documents:
        if doc.id == item.origin_document_id or doc.title == item.title:
            kws.append(doc.why_needed)
            kws.extend(doc.search_queries)
    return [k for k in kws if k and str(k).strip()]


def rebuild_index(case_id: str) -> dict:
    state = ingest_library(case_id)
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


async def summarize_item(state: CaseState, item: KnowledgeItem) -> KnowledgeItem:
    text = _item_text(item)
    if not text.strip():
        item.summary_status = "failed"
        item.summary_error = "Нет текста для саммари"
        return item

    chunks = chunk_text(text)
    picked = pick_relevant_chunks(chunks, _keywords(state, item))
    excerpts = "\n\n---\n\n".join(picked)
    user = f"""Тема проверки: {state.inspection_name}
Ключевые слова: {", ".join(state.keywords) or "не указаны"}
Период: {state.period or "не указан"}
Документ: {item.title}
Источник: {item.source}

Фрагменты НПА:
{excerpts}

Верни саммари:
## Зачем этот акт для проверки
## Ключевые нормы (статьи/пункты)
## Что проверять аудитору
## Риски / типичные нарушения
## Чего нет во фрагментах (если пробелы)
"""
    item.summary_status = "running"
    try:
        item.summary = await chat_complete(SUMMARY_SYSTEM, user, timeout=settings.ollama_timeout_sec)
        item.summary_status = "ok"
        item.summary_error = None
        out = _summaries_dir(state.case_id) / f"{_safe_stem(item.filename)}.md"
        out.write_text(f"# {item.title}\n\n{item.summary}\n", encoding="utf-8")
        item.summary_path = str(out)
    except Exception as exc:  # noqa: BLE001
        item.summary_status = "failed"
        item.summary_error = str(exc)
    return item


async def build_knowledge_events(case_id: str) -> AsyncIterator[dict]:
    t0 = datetime.utcnow()

    def elapsed() -> int:
        return int((datetime.utcnow() - t0).total_seconds() * 1000)

    yield {"type": "status", "message": "Извлечение текста из файлов…", "elapsed_ms": elapsed()}
    ingest_library(case_id)
    yield {"type": "status", "message": "Нарезка на чанки…", "elapsed_ms": elapsed()}
    rebuild_index(case_id)
    state = store.get(case_id)

    yield {
        "type": "status",
        "message": f"Саммари по {sum(1 for i in state.knowledge if i.extract_status == 'ok')} документам…",
        "elapsed_ms": elapsed(),
    }
    for item in state.knowledge:
        if item.extract_status != "ok":
            continue
        yield {
            "type": "status",
            "message": f"Саммари: {item.title}",
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

    yield {"type": "status", "message": "Векторный индекс (релевантные фрагменты)…", "elapsed_ms": elapsed()}
    try:
        kws = list(state.keywords) + list(state.topics) + [state.inspection_name]
        index = await embed_index(case_id, kws)
        n = index.get("embedded") or 0
        yield {"type": "status", "message": f"Проиндексировано эмбеддингов: {n}", "elapsed_ms": elapsed()}
    except Exception as exc:  # noqa: BLE001
        yield {
            "type": "status",
            "message": f"Эмбеддинги пропущены ({exc}). Поиск будет по ключевым словам.",
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


def retrieve(case_id: str, question: str, top_k: int | None = None) -> list[dict]:
    top_k = top_k or settings.rag_top_k
    index = _load_index(case_id)
    chunks = index.get("chunks") or []
    if not chunks:
        return []

    q_tokens = set(tokenize(question))
    scored: list[tuple[float, dict]] = []
    q_emb: list[float] | None = None

    for ch in chunks:
        lex = keyword_score(ch["text"], list(q_tokens) + [question])
        # extra: token overlap
        ctok = set(tokenize(ch["text"]))
        if q_tokens:
            lex += 2.0 * len(q_tokens & ctok)
        vec = 0.0
        scored.append((lex, ch))

    # optional vector rerank if query embed is cheap — skipped here to stay sync
    scored.sort(key=lambda x: x[0], reverse=True)
    # blend with cosine if embeddings exist
    has_emb = any(c.get("embedding") for c in chunks)
    if has_emb:
        # lazy: score with dummy, then we'll embed query in ask()
        pass
    return [c for _, c in scored[: max(top_k * 3, top_k)]]


async def ask(case_id: str, question: str, top_k: int | None = None) -> dict:
    top_k = top_k or settings.rag_top_k
    state = store.get(case_id)
    index = _load_index(case_id)
    chunks = index.get("chunks") or []
    if not chunks:
        raise ValueError("База знаний пуста. Сначала соберите индекс.")

    q_tokens = set(tokenize(question))
    q_emb: list[float] | None = None
    try:
        vectors = await embed_texts([question])
        q_emb = vectors[0] if vectors else None
    except Exception:
        q_emb = None

    ranked: list[tuple[float, dict]] = []
    for ch in chunks:
        lex = keyword_score(ch["text"], list(q_tokens) + [question])
        ctok = set(tokenize(ch["text"]))
        if q_tokens:
            lex += 3.0 * len(q_tokens & ctok) / max(1, len(q_tokens))
        vec = 0.0
        if q_emb and ch.get("embedding"):
            vec = cosine(q_emb, ch["embedding"]) * 12.0
        ranked.append((lex + vec, ch))
    ranked.sort(key=lambda x: x[0], reverse=True)
    picked = [c for s, c in ranked[:top_k] if s > 0] or [c for _, c in ranked[:top_k]]

    context_parts = []
    sources = []
    for i, ch in enumerate(picked, start=1):
        context_parts.append(f"[{i}] {ch['title']}\n{ch['text']}")
        sources.append(
            {
                "n": i,
                "title": ch["title"],
                "filename": ch.get("filename"),
                "excerpt": ch["text"][:400],
            }
        )
    user = f"""Тема проверки: {state.inspection_name}
Ключевые слова: {", ".join(state.keywords)}
Вопрос аудитора: {question}

Фрагменты из базы НПА:
{chr(10).join(context_parts)}
"""
    answer = await chat_complete(ASK_SYSTEM, user, timeout=settings.ollama_timeout_sec)
    return {
        "answer": answer,
        "sources": sources,
        "model": settings.ollama_model,
        "used_embeddings": bool(q_emb),
    }


def export_pack_files(case_id: str) -> list[tuple[str, bytes]]:
    """Files for Open WebUI / zip pack: clean texts + summaries + howto."""
    state = store.get(case_id)
    files: list[tuple[str, bytes]] = []
    for item in state.knowledge:
        if item.text_path and Path(item.text_path).exists():
            name = f"docs/{_safe_stem(item.filename)}.txt"
            files.append((name, Path(item.text_path).read_bytes()))
        if item.summary:
            name = f"summaries/{_safe_stem(item.filename)}.md"
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
{ASK_SYSTEM}
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
