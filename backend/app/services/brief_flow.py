from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path

from app.config import settings
from app.services.brief_docx import write_brief_docx
from app.services.document_artifact import (
    ArtifactSpec,
    ElapsedTimer,
    artifact_dir,
    artifact_docx_path,
    artifact_download_name,
    artifact_md_path,
    artifact_sources_path,
    artifact_status,
    event_result,
    resolve_artifact_file,
    save_artifact_meta,
)
from app.services.knowledge_ingest import ingest_library
from app.services.knowledge_flow import OVERVIEW_PROMPT, SUMMARY_SYSTEM
from app.services.knowledge_summarize import (
    FRAGMENTS_PER_ITEM,
    fragments_from_item,
    summarize_item,
)
from app.services.ollama_client import chat_complete
from app.models import CaseState
from app.storage import store

BRIEF_SCHEMA = 3
BRIEF_SPEC = ArtifactSpec(
    meta_key="brief",
    directory="summaries",
    file_prefix="sammari",
    md_name="brief.md",
    sources_name="brief_sources.json",
    download_suffix="summary",
    docx_endpoint="/api/v1/cases/{case_id}/knowledge/summary.docx",
    md_endpoint="/api/v1/cases/{case_id}/knowledge/summary.md",
    docx_glob="sammari_*.docx",
)


def _section_after(md: str, marker: str) -> str:
    idx = (md or "").find(marker)
    if idx < 0:
        return ""
    section = md[idx + len(marker) :]
    nxt = re.search(r"\n##\s+", section)
    if nxt:
        section = section[: nxt.start()]
    return section


def _markdown_bullets(md: str) -> list[str]:
    chunks: list[str] = []
    for marker in ("## Ключевые нормы", "## Что проверять", "## Основные положения"):
        piece = _section_after(md, marker)
        if piece:
            chunks.append(piece)
    section = "\n".join(chunks) if chunks else (md or "")
    out: list[str] = []
    for ln in section.splitlines():
        s = ln.strip()
        if s.startswith("- ") or s.startswith("* "):
            item = s[2:].strip()
            if item.startswith("…"):
                continue
            out.append(item)
    return out


def _brief_dir(case_id: str) -> Path:
    return artifact_dir(case_id, BRIEF_SPEC)


def _docx_path(case_id: str, inspection_name: str) -> Path:
    return artifact_docx_path(case_id, inspection_name, BRIEF_SPEC)


def _md_path(case_id: str) -> Path:
    return artifact_md_path(case_id, BRIEF_SPEC)


def _sources_path(case_id: str) -> Path:
    return artifact_sources_path(case_id, BRIEF_SPEC)


def brief_download_name(inspection_name: str, case_id: str = "", ext: str = "docx") -> str:
    _ = case_id
    return artifact_download_name(inspection_name, BRIEF_SPEC, ext=ext)


def resolve_brief_file(case_id: str, kind: str) -> Path | None:
    return resolve_artifact_file(case_id, BRIEF_SPEC, kind)


def brief_status(case_id: str) -> dict:
    return artifact_status(case_id, BRIEF_SPEC)


def _brief_stale(state: CaseState) -> bool:
    meta = state.meta.get("brief") or {}
    path = Path(meta["docx_path"]) if meta.get("docx_path") else None
    if not path or not path.exists():
        return True
    ok_items = sum(1 for i in state.knowledge if i.extract_status == "ok")
    if meta.get("items") != ok_items:
        return True
    if meta.get("schema") != BRIEF_SCHEMA:
        return True
    return False


def collect_brief_sources(state) -> list[dict]:
    sources: list[dict] = []
    n = 1
    for item in state.knowledge:
        if item.extract_status != "ok":
            continue
        frags = fragments_from_item(state, item, start_n=n)[:FRAGMENTS_PER_ITEM]
        if not frags:
            continue
        for i, fr in enumerate(frags):
            fr["n"] = n + i
            sources.append(fr)
        n += len(frags)
    return sources


def _synthesize(state, chapters: list[dict]) -> str:
    n = len(chapters)
    lines = [
        f"К проверке «{state.inspection_name}» приложено {n} документ(ов) из базы знаний.",
        "Каждая карточка ниже — существенное для этой проверки, не перечень всех статей акта.",
        "",
        "## Состав нормативной базы",
    ]
    for ch in chapters:
        bullets = _markdown_bullets(ch.get("body") or "")
        lines.append(f"- **{ch['title']}** — в карточке {len(bullets)} опор.")
    lines.append("")
    lines.append("## Основные моменты по актам")
    for ch in chapters:
        lines.append(f"### {ch['title']}")
        bullets = _markdown_bullets(ch.get("body") or "")
        preview = bullets[:12]
        if preview:
            lines.extend(f"- {b}" for b in preview)
            extra = len(bullets) - len(preview)
            if extra > 0:
                lines.append(f"- … ещё {extra} опор в карточке акта ниже")
        else:
            first = next(
                (
                    ln.strip()
                    for ln in (ch.get("body") or "").splitlines()
                    if ln.strip() and not ln.startswith("#")
                ),
                "",
            )
            lines.append(f"- {first[:240]}" if first else "- карточка акта ниже")
        lines.append("")
    return "\n".join(lines).strip()


async def _synthesize_overview(state, chapters: list[dict]) -> str:
    cards = "\n\n".join(f"# {ch['title']}\n{ch['body']}" for ch in chapters)
    try:
        text = await chat_complete(
            SUMMARY_SYSTEM,
            OVERVIEW_PROMPT.format(
                inspection=state.inspection_name,
                keywords=", ".join(state.keywords) or "не указаны",
                cards=cards[:20000],
            ),
            temperature=0.1,
            timeout=settings.summary_timeout_sec,
            num_predict=4096,
        )
        if (text or "").strip():
            return text.strip()
    except Exception:
        pass
    return _synthesize(state, chapters)


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
        "Карточка по каждому акту — существенное для этой проверки, не перечень всех статей. "
        "Номера статей — из текста актов. Официальный URL — страница скачивания.",
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
    return save_artifact_meta(
        state,
        BRIEF_SPEC,
        docx=docx,
        md=md,
        sources=sources,
        body=body,
        extra={
            "items": sum(1 for i in state.knowledge if i.extract_status == "ok"),
            "schema": BRIEF_SCHEMA,
        },
    )


async def build_brief_events(case_id: str, force: bool = False) -> AsyncIterator[dict]:
    timer = ElapsedTimer()
    elapsed = timer.ms

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

    yield {"type": "status", "message": "Готовлю карточки существенного по актам…", "elapsed_ms": elapsed()}
    sources = collect_brief_sources(state)

    total = len(ok_items)
    for idx, item in enumerate(ok_items, start=1):
        status_q: asyncio.Queue[str] = asyncio.Queue()

        async def on_status(msg: str, q: asyncio.Queue[str] = status_q) -> None:
            await q.put(msg)

        yield {
            "type": "status",
            "message": f"Читаю акт {idx} из {total}: {item.title}",
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
        chapters.append({"title": item.title, "body": body, "item_id": item.id})

    yield {"type": "status", "message": "Собираю обзор проверки по карточкам актов…", "elapsed_ms": elapsed()}
    overview = await _synthesize_overview(state, chapters)

    body_for_pages = overview + "\n\n" + "\n\n".join(ch["body"] for ch in chapters)
    md = _md_path(case_id)
    docx = _docx_path(case_id, state.inspection_name)
    yield {"type": "status", "message": "Собираю Word с карточками актов…", "elapsed_ms": elapsed()}
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
    return await event_result(build_brief_events(case_id, force=force), "Саммари не собрано")
