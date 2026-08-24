from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from pathlib import Path

from app.config import settings
from app.filenames import safe_stem
from app.models import CaseState, KnowledgeItem
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

SUMMARY_SYSTEM = """Ты — конспектировщик нормативных правовых актов Республики Беларусь.
Тебе дают текст акта (целиком или очередную часть по порядку). Задача — выписать основные положения так, чтобы аудитору не нужно было открывать первоисточник.

Правила:
1. Только переданный текст. Не выдумывай статьи, пункты, сроки, ставки и цифры.
2. Иди по тексту сверху вниз. Не прыгай и не выбирай «интересные» абзацы.
3. Не фильтруй по теме проверки и не сужай до ключевых слов. Тема проверки тебя здесь не касается.
4. Каждая статья / пункт / глава из куска, в которой есть норма (определение, обязанность, право, запрет, условие, срок, исключение, ответственность, перечень документов), должна попасть в список.
5. У каждого пункта указывай номер статьи / пункта / главы, если он есть в тексте.
6. Пиши по-русски, официально, маркированным списком. Без вступления, без заключения, без норм РФ/ЕС/IFRS, которых нет в тексте.
7. Если кусок — часть длинного акта, не делай вид, что это весь документ.
"""

PASSPORT_PROMPT = """Ниже начало документа «{title}» (преамбула и первые положения).

Текст:
{body}

Выпиши ПАСПОРТ акта списком, только из этого текста:
- полное название и вид акта;
- на кого распространяется;
- что регулирует;
- с какой даты действует / что отменяет — только если сказано.

Объём: 500–1200 знаков. Не перечисляй все статьи — только «о чём документ».
"""

MAP_SECTION_PROMPT = """Это часть {idx} из {total} документа «{title}».
Прочитай этот кусок ЦЕЛИКОМ, по порядку. Не ссылайся на текст вне этой части.

Заголовки статей/глав в ЭТОМ куске (каждая должна появиться в списке, если в ней есть норма):
{outline}

Текст части:
{body}

Выпиши ОСНОВНЫЕ ПОЛОЖЕНИЯ этого куска. Одна статья/пункт — один или несколько пунктов списка.

Формат строго:
- ст./п./гл. N — суть нормы (1–3 предложения). Сохрани числа, сроки, проценты, запреты, перечни документов.

Это перечень, а не «выжимка из трёх главных мыслей». Пропуск статьи из оглавления куска — ошибка. Без вступления.
"""

RETRY_SECTION_PROMPT = """Предыдущий конспект части {idx} из {total} документа «{title}» слишком короткий и пропускает статьи.

Заголовки, которые ОБЯЗАНЫ быть в списке:
{outline}

Твой предыдущий ответ:
{previous}

Текст части ещё раз:
{body}

Выпиши заново полный перечень положений. Формат: `- ст./п. N — суть`. Не сжимай.
"""

ONESHOT_PROMPT = """Ниже текст документа «{title}» ЦЕЛИКОМ. Прочитай его от начала до конца, не выборочно.
Источник: {source}

Статьи/главы из текста (каждая с нормой должна быть в списке основных положений):
{outline}

Полный текст акта:
{body}

Верни markdown:

## Назначение и сфера действия
На кого распространяется, что регулирует, с какого момента — только если это есть в тексте.

## Основные положения
Перечень основных моментов ВСЕГО акта по порядку. Не 3–5 «самых важных», а все существенные нормы: определения, обязанности, запреты, права, условия, сроки, исключения, ответственность. У каждой позиции — статья/пункт.

Только списки. Без воды.
"""

ASK_SYSTEM = """Ты — ассистент внутреннего аудитора банка РБ.
Отвечай на вопрос, опираясь на конспекты актов (основные положения по полному тексту) и на дословные фрагменты из базы знаний.
Правила:
1. Сначала смотри конспект акта, затем дословный фрагмент. Цитируй документ и, если есть, статью/пункт.
2. Если в конспектах и фрагментах нет ответа — так и скажи. Можно добавить общую оговорку из своих знаний, явно пометив «не из базы НПА».
3. Не выдумывай номера норм.
"""


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
    if not (piece or "").strip():
        return True
    bullets = _bullet_count(piece)
    if headings:
        return bullets < max(1, (len(headings) + 1) // 2)
    return bullets < 2


def _assemble_document_summary(
    *,
    title: str,
    text: str,
    passport: str | None,
    parts: list[tuple[str, str]],
) -> str:
    outline = extract_article_outline(text)
    header = f"Конспект покрывает весь текст «{title}» ({len(text)} знаков"
    if outline:
        header += f", {len(outline)} заголовков статей/глав"
    header += "). Части прочитаны по порядку, без отбора по поиску."
    lines = [header, ""]
    if (passport or "").strip():
        lines.append("## Назначение и сфера действия")
        lines.append(passport.strip())
        lines.append("")
    if outline:
        shown = outline[:120]
        lines.append("## Структура акта")
        lines.extend(f"- {h}" for h in shown)
        if len(outline) > 120:
            lines.append(f"- … ещё {len(outline) - 120} заголовков в первоисточнике")
        lines.append("")
    lines.append("## Основные положения")
    for heading, body in parts:
        lines.append(f"### {heading}")
        lines.append((body or "").strip() or "_модель не вернула положения этой части_")
        lines.append("")
    return "\n".join(lines).strip()


def _fragments_from_item(
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


def _format_fragment_block(fragments: list[dict]) -> str:
    blocks = []
    for fr in fragments:
        article = fr.get("article") or "фрагмент без номера статьи"
        url = fr.get("url") or "URL в библиотеке не зафиксирован"
        blocks.append(
            f"[{fr['n']}] {article}\nисточник: {url}\n{fr.get('text') or fr.get('excerpt') or ''}"
        )
    return "\n\n---\n\n".join(blocks)


async def _chat_summary(user: str) -> str:
    return await chat_complete(
        SUMMARY_SYSTEM,
        user,
        temperature=0.1,
        timeout=settings.summary_timeout_sec,
        num_ctx=settings.ollama_num_ctx,
        num_predict=8192,
    )


async def _notes_for_window(
    item: KnowledgeItem,
    window: str,
    idx: int,
    total: int,
) -> str:
    outline = _format_outline(window, limit=80)
    headings = extract_article_outline(window)
    try:
        piece = await _chat_summary(
            MAP_SECTION_PROMPT.format(
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
                RETRY_SECTION_PROMPT.format(
                    idx=idx,
                    total=total,
                    title=item.title,
                    outline=outline,
                    previous=(piece or "")[:4000],
                    body=window,
                )
            )
        except Exception:
            pass
    if not (piece or "").strip():
        return f"[Часть {idx} из {total}: модель вернула пустой конспект.]"
    return piece.strip()


async def _summarize_full_document(
    state: CaseState,
    item: KnowledgeItem,
    text: str,
    on_status: Progress = None,
) -> str:
    windows = sequential_windows(text)

    if len(windows) <= 1:
        if on_status:
            await on_status(f"Читаю целиком и выписываю положения: {item.title}")
        return await _chat_summary(
            ONESHOT_PROMPT.format(
                title=item.title,
                source=item.source,
                outline=_format_outline(text),
                body=text,
            )
        )

    total = len(windows)
    passport = ""
    preamble = text[: min(5500, len(windows[0]) + 400)]
    if on_status:
        await on_status(f"Паспорт акта: {item.title}")
    try:
        passport = await _chat_summary(
            PASSPORT_PROMPT.format(title=item.title, body=preamble)
        )
    except Exception:
        passport = ""

    parts: list[tuple[str, str]] = []
    for idx, window in enumerate(windows, start=1):
        label = _window_label(window, idx, total)
        if on_status:
            await on_status(
                f"Выписываю положения «{item.title}»: {idx} из {total} (по порядку, без RAG)"
            )
        piece = await _notes_for_window(item, window, idx, total)
        parts.append((label, piece))

    if on_status:
        await on_status(f"Склеиваю перечень положений без сжатия: {item.title}")
    return _assemble_document_summary(
        title=item.title,
        text=text,
        passport=passport,
        parts=parts,
    )


async def summarize_item(
    state: CaseState,
    item: KnowledgeItem,
    fragments: list[dict] | None = None,
    on_status: Progress = None,
) -> KnowledgeItem:
    """Summarize the full extracted text as a sequential listing of provisions.
    `fragments` are citations only, never the reading set.
    """
    text = _item_text(item)
    if not text.strip():
        item.summary_status = "failed"
        item.summary_error = "Нет текста для саммари"
        return item

    frags = fragments if fragments is not None else _fragments_from_item(state, item)
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
        "message": f"Конспекты по полным текстам: {sum(1 for i in state.knowledge if i.extract_status == 'ok')} документов…",
        "elapsed_ms": elapsed(),
    }
    for item in state.knowledge:
        if item.extract_status != "ok":
            continue
        yield {
            "type": "status",
            "message": f"Выписываю положения: {item.title}",
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
    user = f"""Тема проверки: {state.inspection_name}
Ключевые слова: {", ".join(state.keywords)}
Вопрос аудитора: {question}

{summary_block}Дословные фрагменты, ближайшие к вопросу:
{chr(10).join(context_parts)}
"""
    answer = await chat_complete(ASK_SYSTEM, user, timeout=settings.ollama_timeout_sec)
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
