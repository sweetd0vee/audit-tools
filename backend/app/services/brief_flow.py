from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

from app.config import settings
from app.filenames import safe_stem
from app.services.brief_docx import write_brief_docx
from app.services.citations import extract_article_outline, pages_estimate
from app.services.knowledge_flow import (
    FRAGMENTS_PER_ITEM,
    _fragments_from_item,
    _item_text,
    ingest_library,
    summarize_item,
)
from app.models import CaseState
from app.services.ollama_client import chat_complete
from app.storage import store

SYNTHESIS_SYSTEM = """Ты — старший аудитор банка в Республике Беларусь.
По ПОЛНЫМ конспектам актов из библиотеки напиши навигационный обзор.
Правила:
1. Только то, что есть в конспектах. Не выдумывай статьи.
2. Обзор — оглавление основных моментов по каждому акту, не замена карточек.
3. Не подменяй право РБ нормами РФ/ЕС/IFRS.
4. Это черновик для чтения глазами, не аудиторское суждение.
"""


def _brief_dir(case_id: str) -> Path:
    path = store.case_dir(case_id) / "summaries"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _docx_path(case_id: str, inspection_name: str) -> Path:
    stem = safe_stem(inspection_name or "proverka")
    return _brief_dir(case_id) / f"sammari_{stem}_{case_id}.docx"


def _md_path(case_id: str) -> Path:
    return _brief_dir(case_id) / "brief.md"


def _sources_path(case_id: str) -> Path:
    return _brief_dir(case_id) / "brief_sources.json"


def brief_download_name(inspection_name: str, case_id: str = "", ext: str = "docx") -> str:
    _ = case_id
    stem = safe_stem(inspection_name or "proverka")
    suffix = (ext or "docx").lstrip(".")
    if suffix == "md":
        return f"{stem}_summary.md"
    return f"{stem}_summary.{suffix}"


def resolve_brief_file(case_id: str, kind: str) -> Path | None:
    state = store.get(case_id)
    meta = state.meta.get("brief") or {}
    key = "docx_path" if kind == "docx" else "md_path"
    stored = meta.get(key)
    if stored and Path(stored).exists():
        return Path(stored)
    if kind == "docx":
        candidate = _docx_path(case_id, state.inspection_name)
        if candidate.exists():
            return candidate
        found = sorted(_brief_dir(case_id).glob("sammari_*.docx"))
        return found[-1] if found else None
    candidate = _md_path(case_id)
    return candidate if candidate.exists() else None


def brief_status(case_id: str) -> dict:
    state = store.get(case_id)
    meta = dict(state.meta.get("brief") or {})
    docx = Path(meta["docx_path"]) if meta.get("docx_path") else _docx_path(case_id, state.inspection_name)
    ready = docx.exists()
    meta.update(
        {
            "case_id": case_id,
            "ready": ready,
            "docx_path": str(docx) if ready else meta.get("docx_path"),
            "download": f"/api/v1/cases/{case_id}/knowledge/summary.docx",
            "markdown": f"/api/v1/cases/{case_id}/knowledge/summary.md",
            "inspection_name": state.inspection_name,
        }
    )
    return meta


def _brief_stale(state: CaseState) -> bool:
    meta = state.meta.get("brief") or {}
    path = Path(meta["docx_path"]) if meta.get("docx_path") else None
    if not path or not path.exists():
        return True
    ok_items = sum(1 for i in state.knowledge if i.extract_status == "ok")
    if meta.get("items") != ok_items:
        return True
    return False


def collect_brief_sources(state) -> list[dict]:
    sources: list[dict] = []
    n = 1
    for item in state.knowledge:
        if item.extract_status != "ok":
            continue
        frags = _fragments_from_item(state, item, start_n=n)[:FRAGMENTS_PER_ITEM]
        if not frags:
            continue
        for i, fr in enumerate(frags):
            fr["n"] = n + i
            sources.append(fr)
        n += len(frags)
    return sources


async def _synthesize(state, chapters: list[dict]) -> str:
    cards = "\n\n".join(f"# {ch['title']}\n{ch['body']}" for ch in chapters)
    user = f"""Тема проверки: {state.inspection_name}
Ключевые слова: {", ".join(state.keywords) or "не указаны"}
Период: {state.period or "не указан"}

Конспекты актов (каждый покрывает документ целиком):
{cards}

Напиши навигационный обзор — перечень основных моментов, чтобы можно было сразу перейти к нужной карточке ниже. Не сокращай карточки: они уже есть в файле.

## Состав нормативной базы
По каждому акту: одно предложение, о чём документ и зачем он в этой проверке.

## Основные моменты по актам
Для КАЖДОГО акта отдельный подзаголовок ### и маркированный список 6–15 главных положений с номерами статей/пунктов из конспекта. Не пропускай акт.

## Пересечения норм
Только если это явно следует из конспектов.

Не выдумывай статьи. Нормы РФ/ЕС/IFRS не подставляй.
"""
    return await chat_complete(
        SYNTHESIS_SYSTEM,
        user,
        timeout=settings.summary_timeout_sec,
        num_ctx=settings.ollama_num_ctx,
        num_predict=8192,
    )


def _write_markdown(
    path: Path,
    *,
    inspection_name: str,
    period: str | None,
    keywords: list[str],
    case_id: str,
    overview: str,
    chapters: list[dict],
    sources: list[dict],
) -> None:
    lines = [
        f"# Саммари нормативной базы",
        f"**{inspection_name}**",
        f"Период: {period or 'не указан'}. Ключевые слова: {', '.join(keywords) or '—'}. Кейс `{case_id}`.",
        "",
        "Номера статей — из текста актов. Официальный URL — страница скачивания. "
        "Конспект покрывает документ целиком, не выборку по поиску.",
        "",
        "## Обзор проверки",
        overview.strip(),
        "",
    ]
    for ch in chapters:
        lines.append(f"## {ch['title']}")
        lines.append(ch.get("body") or "")
        lines.append("")
    lines.append("## Источники: статьи и фрагменты")
    for src in sources:
        article = src.get("article") or "фрагмент"
        url = src.get("url") or ""
        url_line = f" {url}" if url else f" файл `{src.get('filename') or ''}`"
        lines.append(f"### [{src['n']}] {src.get('title')} — {article}")
        lines.append(url_line.strip())
        lines.append("")
        lines.append(f"> {src.get('excerpt') or ''}")
        lines.append("")
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def _digest(chapters: list[dict], limit: int = 6) -> list[str]:
    out = []
    for ch in chapters[:limit]:
        body = re.sub(r"^#+\s*", "", (ch.get("body") or "").strip(), flags=re.M)
        first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
        if len(first) > 160:
            first = first[:157] + "…"
        out.append(f"- **{ch['title']}**: {first or 'карточка готова'}")
    return out


def _save_brief_meta(state, *, docx: Path, md: Path, sources: list[dict], body: str) -> dict:
    meta = {
        "built_at": datetime.utcnow().isoformat(),
        "docx_path": str(docx),
        "md_path": str(md),
        "items": sum(1 for i in state.knowledge if i.extract_status == "ok"),
        "citations": len(sources),
        "chars": len(body),
        "pages_estimate": pages_estimate(body, settings.brief_chars_per_page),
        "download": f"/api/v1/cases/{state.case_id}/knowledge/summary.docx",
        "markdown": f"/api/v1/cases/{state.case_id}/knowledge/summary.md",
        "ready": True,
        "case_id": state.case_id,
        "inspection_name": state.inspection_name,
    }
    state.meta["brief"] = meta
    store.save(state)
    return meta


async def build_brief_events(case_id: str, force: bool = False) -> AsyncIterator[dict]:
    t0 = datetime.utcnow()

    def elapsed() -> int:
        return int((datetime.utcnow() - t0).total_seconds() * 1000)

    yield {"type": "status", "message": "Собираю тексты библиотеки…", "elapsed_ms": elapsed()}
    state = ingest_library(case_id)
    ok_items = [i for i in state.knowledge if i.extract_status == "ok"]
    if not ok_items:
        raise ValueError("База знаний пуста. Сначала утвердите акты и дождитесь скачивания.")

    if not force and not _brief_stale(state):
        meta = brief_status(case_id)
        yield {"type": "status", "message": "Саммари уже собрано — отдаю файл.", "elapsed_ms": elapsed()}
        yield {"type": "result", **meta, "digest": [], "elapsed_ms": elapsed()}
        return

    yield {"type": "status", "message": "Готовлю конспекты по полным текстам актов…", "elapsed_ms": elapsed()}
    sources = collect_brief_sources(state)

    total = len(ok_items)
    for idx, item in enumerate(ok_items, start=1):
        status_q: asyncio.Queue[str] = asyncio.Queue()

        async def on_status(msg: str, q: asyncio.Queue[str] = status_q) -> None:
            await q.put(msg)

        yield {
            "type": "status",
            "message": f"Читаю акт {idx} из {total} целиком: {item.title}",
            "elapsed_ms": elapsed(),
        }
        task = asyncio.create_task(summarize_item(state, item, on_status=on_status))
        while True:
            if task.done() and status_q.empty():
                break
            try:
                msg = await asyncio.wait_for(status_q.get(), timeout=0.5)
                yield {"type": "status", "message": msg, "elapsed_ms": elapsed()}
            except asyncio.TimeoutError:
                if task.done():
                    break
        await task
        store.save(state)

    state = store.get(case_id)
    chapters = []
    for item in state.knowledge:
        if item.extract_status != "ok":
            continue
        body = (item.summary or "").strip()
        if item.summary_status != "ok" or not body:
            failed = item.summary_error or "модель не вернула текст"
            body = (
                f"Карточка не собрана ({failed}). "
                f"Читайте первоисточник в библиотеке кейса."
            )
        outline = extract_article_outline(_item_text(item))
        if outline:
            shown = outline[:100]
            extra = f"\n- … ещё {len(outline) - 100} разделов в первоисточнике" if len(outline) > 100 else ""
            toc = "### Оглавление акта (из текста)\n" + "\n".join(f"- {h}" for h in shown) + extra
            body = toc + "\n\n" + body
        chapters.append({"title": item.title, "body": body, "item_id": item.id})

    yield {"type": "status", "message": "Пишу сводку основных моментов по всем актам…", "elapsed_ms": elapsed()}
    try:
        overview = await _synthesize(state, chapters)
    except Exception as exc:  # noqa: BLE001
        overview = (
            f"Обзор моделью не собран ({exc}). Ниже — карточки по актам из библиотеки. "
            "Цитаты в приложении сохранены."
        )

    body_for_pages = overview + "\n\n" + "\n\n".join(ch["body"] for ch in chapters)
    md = _md_path(case_id)
    docx = _docx_path(case_id, state.inspection_name)
    yield {"type": "status", "message": "Собираю Word с конспектами актов…", "elapsed_ms": elapsed()}
    _write_markdown(
        md,
        inspection_name=state.inspection_name,
        period=state.period,
        keywords=state.keywords,
        case_id=case_id,
        overview=overview,
        chapters=chapters,
        sources=sources,
    )
    write_brief_docx(
        docx,
        inspection_name=state.inspection_name,
        period=state.period,
        keywords=state.keywords,
        case_id=case_id,
        overview=overview,
        chapters=chapters,
        sources=sources,
    )
    _sources_path(case_id).write_text(
        json.dumps(sources, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    meta = _save_brief_meta(state, docx=docx, md=md, sources=sources, body=body_for_pages)
    meta["digest"] = _digest(chapters)
    yield {"type": "result", **meta, "elapsed_ms": elapsed()}


async def build_brief(case_id: str, force: bool = False) -> dict:
    result: dict | None = None
    async for event in build_brief_events(case_id, force=force):
        if event.get("type") == "result":
            result = event
    if not result:
        raise ValueError("Саммари не собрано")
    return result
