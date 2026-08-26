from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from pathlib import Path

from app.config import settings
from app.models import CaseState
from app.services.brief_docx import write_program_docx
from app.services.brief_flow import collect_brief_sources
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
from app.services.ollama_client import chat_complete
from app.prompts import prompt
from app.storage import store

PROGRAM_SPEC = ArtifactSpec(
    meta_key="program",
    directory="programs",
    file_prefix="programma",
    md_name="program.md",
    sources_name="program_sources.json",
    download_suffix="programma",
    docx_endpoint="/api/v1/cases/{case_id}/knowledge/program.docx",
    md_endpoint="/api/v1/cases/{case_id}/knowledge/program.md",
    docx_glob="programma_*.docx",
)

PROGRAM_ITEMS_FLOOR = 3
PROGRAM_ITEMS_CEILING = 20
DEFAULT_ITEMS_MIN = 8
DEFAULT_ITEMS_MAX = 11

_ITEM_LINE_RE = re.compile(r"^(?:#{1,3}\s*)?(\d{1,2})[.)]\s+(.*)$")
_ITEMS_SPEC_RE = re.compile(r"^\s*(\d{1,2})\s*[-–—]\s*(\d{1,2})\s*$")
_ITEM_ONE_RE = re.compile(r"^\s*(\d{1,2})\s*$")
_QUESTIONS_HEADING_RE = re.compile(r"вопросы,\s*подлежащие\s*аудиту", re.I)


def _clamp_items(n: int) -> int:
    return max(PROGRAM_ITEMS_FLOOR, min(PROGRAM_ITEMS_CEILING, n))


def parse_program_items_spec(value: str | None) -> tuple[int | None, int | None]:
    if not (value or "").strip():
        return None, None
    text = re.sub(r"\s+", "", (value or "").strip())
    ranged = re.fullmatch(r"(\d{1,2})[-–—](\d{1,2})", text)
    if ranged:
        lo, hi = _clamp_items(int(ranged.group(1))), _clamp_items(int(ranged.group(2)))
        if lo > hi:
            lo, hi = hi, lo
        return lo, hi
    one = re.fullmatch(r"(\d{1,2})", text)
    if one:
        n = _clamp_items(int(one.group(1)))
        return n, n
    return None, None


def normalize_program_item_range(
    items_min: int | None = None,
    items_max: int | None = None,
    items: str | None = None,
) -> tuple[int, int]:
    spec_min, spec_max = parse_program_items_spec(items)
    if spec_min is not None:
        items_min = spec_min if items_min is None else items_min
        items_max = spec_max if items_max is None else items_max
    if items_min is None and items_max is None:
        return DEFAULT_ITEMS_MIN, DEFAULT_ITEMS_MAX
    if items_min is None:
        items_min = items_max
    if items_max is None:
        items_max = items_min
    lo, hi = _clamp_items(int(items_min)), _clamp_items(int(items_max))
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def program_items_hint(items_min: int, items_max: int) -> str:
    if items_min == items_max:
        return f"ровно {items_min}"
    return (
        f"от {items_min} до {items_max} "
        f"(компактная проверка ближе к {items_min}, широкая — к {items_max})"
    )


def parse_program_heading(body: str, heading: str) -> str:
    marker = heading.strip().lower()
    capturing = False
    out: list[str] = []
    for line in (body or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            title = stripped[3:].strip().lower()
            if capturing:
                break
            capturing = title == marker or title.startswith(marker)
            continue
        if capturing and stripped:
            out.append(stripped)
    return " ".join(out).strip()


def parse_program_questions(body: str) -> list[str]:
    lines = (body or "").splitlines()
    start = 0
    for i, line in enumerate(lines):
        if _QUESTIONS_HEADING_RE.search(line):
            start = i + 1
            break
    items: list[str] = []
    current: str | None = None
    for line in lines[start:]:
        stripped = line.strip()
        if stripped.startswith("## ") and items:
            break
        numbered = _ITEM_LINE_RE.match(stripped)
        if not numbered and stripped.startswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if (
                len(cells) >= 2
                and re.match(r"^\d{1,2}\.?$", cells[0])
                and not re.match(r"(?i)^№", cells[0])
            ):
                numbered = _ITEM_LINE_RE.match(f"{cells[0].rstrip('.')}. {cells[1]}")
        if numbered:
            if current:
                items.append(current)
            current = numbered.group(2).strip()
            continue
        if current and stripped and not stripped.startswith("| ---") and "---" not in stripped[:8]:
            if stripped.startswith("|"):
                continue
            current = f"{current} {stripped}"
    if current:
        items.append(current)
    return [item for item in items if item]


def _program_dir(case_id: str) -> Path:
    return artifact_dir(case_id, PROGRAM_SPEC)


def _docx_path(case_id: str, inspection_name: str) -> Path:
    return artifact_docx_path(case_id, inspection_name, PROGRAM_SPEC)


def _md_path(case_id: str) -> Path:
    return artifact_md_path(case_id, PROGRAM_SPEC)


def _sources_path(case_id: str) -> Path:
    return artifact_sources_path(case_id, PROGRAM_SPEC)


def program_download_name(inspection_name: str, case_id: str = "", ext: str = "docx") -> str:
    _ = case_id
    return artifact_download_name(inspection_name, PROGRAM_SPEC, ext=ext)


def resolve_program_file(case_id: str, kind: str) -> Path | None:
    return resolve_artifact_file(case_id, PROGRAM_SPEC, kind)


def program_status(case_id: str) -> dict:
    return artifact_status(case_id, PROGRAM_SPEC)


def _program_stale(state: CaseState) -> bool:
    meta = state.meta.get("program") or {}
    path = Path(meta["docx_path"]) if meta.get("docx_path") else None
    if not path or not path.exists():
        return True
    ok_items = sum(1 for i in state.knowledge if i.extract_status == "ok")
    if meta.get("items") != ok_items:
        return True
    if meta.get("keywords") != list(state.keywords):
        return True
    if meta.get("inspection_name") != state.inspection_name:
        return True
    return False


def _document_catalog(state: CaseState) -> str:
    lines = []
    for i, doc in enumerate(state.documents, start=1):
        if not doc.selected and doc.download_status in (None, "skipped"):
            continue
        status = "скачан" if doc.download_status == "ok" else "в списке, не скачан"
        why = doc.why_needed or ""
        lines.append(f"{i}. {doc.title} [{status}]. {why}".strip())
    for item in state.knowledge:
        if item.source == "uploaded":
            lines.append(f"- Приложен файл: {item.title} ({item.filename})")
    return "\n".join(lines) if lines else "Документы ещё не приложены."


def _existing_cards(state: CaseState) -> str:
    blocks = []
    for item in state.knowledge:
        if item.summary_status == "ok" and (item.summary or "").strip():
            body = item.summary.strip()
            if len(body) > 8000:
                body = body[:8000] + "\n…"
            blocks.append(f"# {item.title}\n{body}")
    return "\n\n".join(blocks)


def _format_sources(sources: list[dict]) -> str:
    blocks = []
    for fr in sources[:40]:
        article = fr.get("article") or "фрагмент без номера статьи"
        url = fr.get("url") or "URL в библиотеке не зафиксирован"
        text = fr.get("text") or fr.get("excerpt") or ""
        blocks.append(f"[{fr['n']}] {article}\nисточник: {url}\n{text}")
    return "\n\n---\n\n".join(blocks) if blocks else "Фрагментов НПА нет."


def _digest(body: str, limit: int = 8) -> list[str]:
    out: list[str] = []
    for line in (body or "").splitlines():
        if line.startswith("### "):
            title = line[4:].strip()
            if title:
                out.append(f"- {title}")
            if len(out) >= limit:
                return out
    if not out:
        for line in (body or "").splitlines():
            if line.startswith("## "):
                out.append(f"- {line[3:].strip()}")
                if len(out) >= limit:
                    break
    return out


def _save_program_meta(
    state: CaseState,
    *,
    docx: Path,
    md: Path,
    sources: list[dict],
    body: str,
) -> dict:
    return save_artifact_meta(
        state,
        PROGRAM_SPEC,
        docx=docx,
        md=md,
        sources=sources,
        body=body,
        extra={
            "items": sum(1 for i in state.knowledge if i.extract_status == "ok"),
            "keywords": list(state.keywords),
        },
    )


def _write_markdown(
    path: Path,
    *,
    inspection_name: str,
    period: str | None,
    keywords: list[str],
    case_id: str,
    body: str,
    sources: list[dict],
) -> None:
    lines = [
        "# Программа аудиторской проверки",
        f"**{inspection_name}**",
        f"Период: {period or 'не указан'}. Ключевые слова: {', '.join(keywords) or '—'}. Кейс `{case_id}`.",
        "",
        "Черновик программы внутренней аудиторской проверки банка РБ. "
        "Номера `[n]` — фрагменты в конце файла.",
        "",
        body.strip(),
        "",
    ]
    if sources:
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


async def _compose_program(state: CaseState, sources: list[dict]) -> str:
    catalog = []
    for src in sources:
        article = src.get("article") or "фрагмент"
        url = src.get("url") or "нет URL"
        catalog.append(f"[{src['n']}] {src.get('title')} — {article} — {url}")
    cards = _existing_cards(state)
    target = 8 * settings.brief_chars_per_page
    cards_block = ""
    if cards:
        cards_block = (
            "\nКарточки актов (если уже собрано саммари — используй как ориентир, "
            "но пиши программу процедур, а не пересказ карточек):\n"
            f"{cards}\n"
        )
    user = prompt(
        "program_user",
        inspection=state.inspection_name,
        keywords=", ".join(state.keywords) or "не указаны",
        period=state.period or "не указан",
        document_catalog=_document_catalog(state),
        catalog="\n".join(catalog) or "список пуст — не выдумывай номера статей как факт",
        fragments=_format_sources(sources),
        cards_block=cards_block,
        sections=prompt("program_sections").strip(),
        target=target,
        target_hi=target + 2500,
    )
    return await chat_complete(
        prompt("program_system"),
        user,
        timeout=settings.brief_timeout_sec,
        num_ctx=settings.ollama_num_ctx,
        num_predict=8192,
    )


async def build_program_events(case_id: str, force: bool = False) -> AsyncIterator[dict]:
    timer = ElapsedTimer()
    elapsed = timer.ms

    yield {"type": "status", "message": "Собираю материалы проверки…", "elapsed_ms": elapsed()}
    state = ingest_library(case_id)

    if not force and not _program_stale(state):
        meta = program_status(case_id)
        yield {
            "type": "status",
            "message": "Программа проверки уже собрана — отдаю файл.",
            "elapsed_ms": elapsed(),
        }
        yield {"type": "result", **meta, "digest": [], "elapsed_ms": elapsed()}
        return

    yield {
        "type": "status",
        "message": "Отбираю фрагменты из приложенных документов…",
        "elapsed_ms": elapsed(),
    }
    sources = collect_brief_sources(state)

    yield {
        "type": "status",
        "message": "Пишу программу аудиторской проверки банка РБ. Это может занять несколько минут…",
        "elapsed_ms": elapsed(),
    }
    try:
        body = await _compose_program(state, sources)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Модель не собрала программу проверки: {exc}") from exc
    if not (body or "").strip():
        raise ValueError("Модель вернула пустую программу проверки.")

    md = _md_path(case_id)
    docx = _docx_path(case_id, state.inspection_name)
    yield {
        "type": "status",
        "message": "Собираю Word с программой проверки…",
        "elapsed_ms": elapsed(),
    }
    _write_markdown(
        md,
        inspection_name=state.inspection_name,
        period=state.period,
        keywords=state.keywords,
        case_id=case_id,
        body=body,
        sources=sources,
    )
    write_program_docx(
        docx,
        inspection_name=state.inspection_name,
        period=state.period,
        keywords=state.keywords,
        case_id=case_id,
        body=body,
        sources=sources,
    )
    _sources_path(case_id).write_text(
        json.dumps(sources, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    meta = _save_program_meta(state, docx=docx, md=md, sources=sources, body=body)
    meta["digest"] = _digest(body)
    yield {"type": "result", **meta, "elapsed_ms": elapsed()}


async def build_program(case_id: str, force: bool = False) -> dict:
    return await event_result(
        build_program_events(case_id, force=force),
        "Программа проверки не собрана",
    )
