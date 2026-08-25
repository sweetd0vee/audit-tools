from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from pathlib import Path

from app.config import settings
from app.filenames import safe_stem
from app.models import CaseState, KnowledgeItem
from app.prompts import prompt
from app.services.chunker import (
    chunk_text,
    cosine,
    even_sample,
    keyword_score,
    sequential_windows,
    tokenize,
)
from app.services.citations import (
    excerpt_for_cite,
    extract_article_outline,
    extract_article_ref,
    origin_url,
)
from app.services.document_artifact import ElapsedTimer
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
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)]|ст\.|статья|п\.)\s+", re.I)


def _format_outline(text: str, limit: int = 160) -> str:
    outline = extract_article_outline(text)
    if not outline:
        return "в тексте нет явных заголовков статей/глав — конспектируй по абзацам по порядку"
    extra = ""
    shown = outline
    if limit and len(outline) > limit:
        shown = outline[:limit]
        extra = f"\n… и ещё {len(outline) - limit} заголовков (они есть в тексте ниже)"
    return "\n".join(f"- {line}" for line in shown) + extra


def _bullet_count(text: str) -> int:
    return sum(1 for ln in (text or "").splitlines() if _BULLET_RE.match(ln))


def _window_label(window: str, idx: int, total: int) -> str:
    outline = extract_article_outline(window)
    if not outline:
        return f"Часть {idx} из {total}"
    if len(outline) == 1 or outline[0] == outline[-1]:
        return f"Часть {idx} из {total}: {outline[0]}"
    return f"Часть {idx} из {total}: {outline[0]} — {outline[-1]}"


def _thin_notes(piece: str, headings: list[str]) -> bool:
    _ = headings
    text = (piece or "").strip()
    if not text:
        return True
    if re.search(r"нет существенн|нет норм по теме|не относит", text, re.I):
        return False
    return _bullet_count(piece) < 1 and len(text) < 240


def _inspection_line(state: CaseState) -> tuple[str, str]:
    inspection = (state.inspection_name or "").strip() or "проверка"
    keywords = ", ".join(state.keywords) if state.keywords else "не указаны"
    return inspection, keywords


def _join_map_notes(parts: list[tuple[str, str]]) -> str:
    blocks = []
    for heading, body in parts:
        blocks.append(f"### {heading}\n{(body or '').strip()}")
    return "\n\n".join(blocks)


def fragments_from_item(
    state: CaseState,
    item: KnowledgeItem,
    start_n: int = 1,
    max_fragments: int | None = None,
) -> list[dict]:
    """Sequential windows of the whole document, not keyword/RAG retrieval."""
    text = _item_text(item)
    if not text.strip():
        return []
    windows = sequential_windows(text)
    cap = max_fragments or FRAGMENTS_PER_ITEM
    picked = windows if len(windows) <= cap else even_sample(windows, cap)
    url = origin_url(state, item)
    out = []
    n = start_n
    for part in picked:
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


_fragments_from_item = fragments_from_item


async def _chat_summary(user: str) -> str:
    return await chat_complete(
        prompt("summary_system"),
        user,
        temperature=0.1,
        timeout=settings.summary_timeout_sec,
        num_ctx=settings.ollama_num_ctx,
        num_predict=8192,
    )


async def _notes_for_window(
    state: CaseState,
    item: KnowledgeItem,
    window: str,
    idx: int,
    total: int,
) -> str:
    inspection, keywords = _inspection_line(state)
    outline = _format_outline(window, limit=40)
    headings = extract_article_outline(window)
    try:
        piece = await _chat_summary(
            prompt(
                "map_essential",
                inspection=inspection,
                keywords=keywords,
                idx=idx,
                total=total,
                title=item.title,
                outline=outline,
                body=window,
            )
        )
    except Exception as exc:  # noqa: BLE001
        return (
            f"[Часть {idx} из {total} не собрана: {exc}. "
            "Этот диапазон акта смотрите в первоисточнике.]"
        )
    if _thin_notes(piece, headings):
        try:
            piece = await _chat_summary(
                prompt(
                    "retry_essential",
                    idx=idx,
                    total=total,
                    title=item.title,
                    inspection=inspection,
                    keywords=keywords,
                    previous=(piece or "")[:3000],
                    body=window,
                )
            )
        except Exception:
            pass
    if not (piece or "").strip():
        return f"[Часть {idx} из {total}: модель вернула пустые заметки.]"
    return piece.strip()


async def _reduce_card(
    state: CaseState,
    item: KnowledgeItem,
    text: str,
    parts: list[tuple[str, str]],
) -> str:
    inspection, keywords = _inspection_line(state)
    notes = _join_map_notes(parts)
    try:
        card = await _chat_summary(
            prompt(
                "reduce_card",
                inspection=inspection,
                keywords=keywords,
                title=item.title,
                chars=len(text),
                parts=len(parts),
                notes=notes[:24000],
            )
        )
    except Exception as extra:  # noqa: BLE001
        return (
            f"Карточка не синтезирована ({extra}). Заметки по частям:\n\n{notes[:8000]}"
        )
    if not (card or "").strip():
        return notes
    card = card.strip()
    if "## Ключевые нормы" not in card and "## Основные положения" not in card:
        card = "## Ключевые нормы\n" + card
    return card


async def _summarize_full_document(
    state: CaseState,
    item: KnowledgeItem,
    text: str,
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

    total = len(windows)
    parts: list[tuple[str, str]] = []
    for idx, window in enumerate(windows, start=1):
        label = _window_label(window, idx, total)
        if on_status:
            await on_status(
                f"Читаю «{item.title}»: часть {idx} из {total} — существенное для проверки"
            )
        piece = await _notes_for_window(state, item, window, idx, total)
        parts.append((label, piece))

    if on_status:
        await on_status(f"Собираю карточку существенного: {item.title}")
    return await _reduce_card(state, item, text, parts)


async def summarize_item(
    state: CaseState,
    item: KnowledgeItem,
    fragments: list[dict] | None = None,
    on_status: Progress | None = None,
) -> KnowledgeItem:
    """Карточка существенного по полному тексту акта (map-reduce), не перечень всех статей.
    `fragments` — только цитаты в приложение Word.
    """
    text = _item_text(item)
    if not text.strip():
        item.summary_status = "failed"
        item.summary_error = "Нет текста для саммари"
        return item

    frags = fragments if fragments is not None else fragments_from_item(state, item)
    item.summary_status = "running"
    item.citations = [
        {
            "n": fr["n"],
            "article": fr.get("article"),
            "excerpt": fr.get("excerpt"),
            "url": fr.get("url"),
            "title": fr.get("title") or item.title,
            "filename": fr.get("filename") or item.filename,
            "item_id": item.id,
        }
        for fr in frags
    ]
    try:
        item.summary = await _summarize_full_document(state, item, text, on_status=on_status)
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


def _diversify_chunks(ranked: list[tuple[float, dict]], top_k: int) -> list[dict]:
    """Cover several documents first, then fill remaining slots from global rank."""
    by_item: dict[str, list[dict]] = {}
    for _score, ch in ranked:
        key = str(ch.get("item_id") or ch.get("title") or "")
        by_item.setdefault(key, []).append(ch)
    picked: list[dict] = []
    seen: set[str] = set()

    def _take(ch: dict) -> bool:
        cid = str(ch.get("id") or id(ch))
        if cid in seen:
            return False
        seen.add(cid)
        picked.append(ch)
        return True

    for group in by_item.values():
        if group:
            _take(group[0])
        if len(picked) >= top_k:
            return picked
    for group in by_item.values():
        if len(group) > 1:
            _take(group[1])
        if len(picked) >= top_k:
            return picked
    for _score, ch in ranked:
        _take(ch)
        if len(picked) >= top_k:
            break
    return picked


def _summary_context(state: CaseState, question: str, budget: int = 18000) -> str:
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
    positive = [(s, c) for s, c in ranked if s > 0] or ranked
    picked = _diversify_chunks(positive, top_k)

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
    summaries = _summary_context(state, question)
    summary_block = (
        f"Конспекты актов (основные положения по полным текстам):\n{summaries}\n\n"
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
        prompt("ask_system"), user, timeout=settings.ollama_timeout_sec
    )
    return {
        "answer": answer,
        "sources": sources,
        "model": settings.ollama_model,
        "used_embeddings": bool(q_emb),
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
