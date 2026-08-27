from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from pathlib import Path

from app.config import settings
from app.models import CaseState
from app.services.brief_docx import write_program_docx
from app.services.brief_flow import collect_brief_sources
from app.services.case_context import document_catalog, existing_cards, format_npa_sources
from app.services.document_artifact import (
    ArtifactSpec,
    ElapsedTimer,
    artifact_download_name,
    artifact_paths,
    artifact_stale,
    artifact_status,
    event_result,
    resolve_artifact_file,
    save_artifact_meta,
)
from app.services.knowledge_ingest import ingest_library
from app.services.ollama_client import chat_complete
from app.prompts import prompt

PROGRAM_SCHEMA = 5
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
        return (
            f"строго {items_min} (ровно {items_min} пунктов, "
            f"без пункта {items_min + 1})"
        )
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
    expected = 1
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
            n = int(numbered.group(1))
            text = numbered.group(2).strip()
            if n == expected:
                if current:
                    items.append(current)
                current = text
                expected += 1
                continue
            if current and stripped:
                current = f"{current} {stripped}"
            continue
        if current and stripped and not stripped.startswith("| ---") and "---" not in stripped[:8]:
            if stripped.startswith("|"):
                continue
            current = f"{current} {stripped}"
    if current:
        items.append(current)
    return [item for item in items if item]


def fit_program_questions(questions: list[str], items_max: int) -> list[str]:
    return [item.strip() for item in questions if (item or "").strip()][:items_max]


def program_download_name(inspection_name: str, case_id: str = "", ext: str = "docx") -> str:
    _ = case_id
    return artifact_download_name(inspection_name, PROGRAM_SPEC, ext=ext)


def resolve_program_file(case_id: str, kind: str) -> Path | None:
    return resolve_artifact_file(case_id, PROGRAM_SPEC, kind)


def program_status(case_id: str) -> dict:
    return artifact_status(case_id, PROGRAM_SPEC)


def _program_stale(
    state: CaseState,
    *,
    items_min: int | None = None,
    items_max: int | None = None,
) -> bool:
    extra: dict = {
        "keywords": list(state.keywords),
        "inspection_name": state.inspection_name,
    }
    if items_min is not None:
        extra["items_min"] = items_min
    if items_max is not None:
        extra["items_max"] = items_max
    return artifact_stale(
        state,
        PROGRAM_SPEC,
        schema=PROGRAM_SCHEMA,
        check_items=True,
        extra=extra,
    )


def _digest(questions: list[str], limit: int = 8) -> list[str]:
    out: list[str] = []
    for idx, question in enumerate(questions, start=1):
        title = re.split(r"[.!?]\s", question, maxsplit=1)[0].strip()
        if len(title) > 120:
            title = title[:117].rstrip() + "…"
        if title:
            out.append(f"- {idx}. {title}")
        if len(out) >= limit:
            return out
    return out


def _save_program_meta(
    state: CaseState,
    *,
    docx: Path,
    md: Path,
    sources: list[dict],
    body: str,
    items_min: int,
    items_max: int,
    questions: list[str],
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
            "schema": PROGRAM_SCHEMA,
            "items_min": items_min,
            "items_max": items_max,
            "question_count": len(questions),
        },
    )


def _write_markdown(
    path: Path,
    *,
    inspection_name: str,
    keywords: list[str],
    case_id: str,
    body: str,
    sources: list[dict],
    questions: list[str],
) -> None:
    lines = [
        "# ПРОГРАММА",
        "",
        f"**Название проверки:** {inspection_name}",
        "**Сроки проведения:**",
        "**Руководитель проверки:**",
        "**Члены рабочей группы:**",
        f"Ключевые слова: {', '.join(keywords) or '—'}. Кейс `{case_id}`.",
        "",
        "Черновик программы внутренней аудиторской проверки банка РБ. "
        "Номера `[n]` — фрагменты в конце файла.",
        "",
        "## Вопросы, подлежащие аудиту",
        "",
        "| № п/п | Вопросы, подлежащие аудиту |",
        "| --- | --- |",
    ]
    for idx, question in enumerate(questions, start=1):
        cell = (question or "").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {idx}. | {cell} |")
    if not questions:
        lines.append("| 1. | |")
        if (body or "").strip():
            lines.extend(["", body.strip()])
    lines.append("")
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


async def _compose_program(
    state: CaseState,
    sources: list[dict],
    *,
    items_min: int,
    items_max: int,
) -> str:
    catalog = []
    for src in sources:
        article = src.get("article") or "фрагмент"
        url = src.get("url") or "нет URL"
        catalog.append(f"[{src['n']}] {src.get('title')} — {article} — {url}")
    cards = existing_cards(state)
    per_item = 600
    target = items_max * per_item
    cards_block = ""
    if cards:
        cards_block = (
            "\nКарточки актов (если уже собрано саммари — используй как ориентир, "
            "но пиши вопросы программы, а не пересказ карточек):\n"
            f"{cards}\n"
        )
    user = prompt(
        "program_user",
        inspection=state.inspection_name,
        keywords=", ".join(state.keywords) or "не указаны",
        document_catalog=document_catalog(state),
        catalog="\n".join(catalog) or "список пуст — не выдумывай номера статей как факт",
        fragments=format_npa_sources(sources),
        cards_block=cards_block,
        sections=prompt("program_sections", items_hint=program_items_hint(items_min, items_max)).strip(),
        items_hint=program_items_hint(items_min, items_max),
        target=target,
        target_hi=target + items_max * 120,
    )
    return await chat_complete(
        prompt("program_system"),
        user,
        timeout=settings.brief_timeout_sec,
        num_ctx=settings.ollama_num_ctx,
        num_predict=min(4096, max(2048, items_max * 320)),
    )


async def build_program_events(
    case_id: str,
    force: bool = False,
    items_min: int | None = None,
    items_max: int | None = None,
    items: str | None = None,
) -> AsyncIterator[dict]:
    timer = ElapsedTimer()
    elapsed = timer.ms
    requested_min, requested_max = items_min, items_max
    if items:
        spec_min, spec_max = parse_program_items_spec(items)
        if spec_min is not None:
            requested_min = spec_min if requested_min is None else requested_min
            requested_max = spec_max if requested_max is None else requested_max
    specified = requested_min is not None or requested_max is not None
    lo, hi = normalize_program_item_range(requested_min, requested_max)

    yield {"type": "status", "message": "Собираю материалы проверки…", "elapsed_ms": elapsed()}
    state = ingest_library(case_id)

    if not force and not _program_stale(
        state,
        items_min=lo if specified else None,
        items_max=hi if specified else None,
    ):
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

    hint = program_items_hint(lo, hi)
    yield {
        "type": "status",
        "message": f"Пишу программу аудиторской проверки банка РБ ({hint} пунктов). Это может занять несколько минут…",
        "elapsed_ms": elapsed(),
    }
    try:
        body = await _compose_program(state, sources, items_min=lo, items_max=hi)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Модель не собрала программу проверки: {exc}") from exc
    if not (body or "").strip():
        raise ValueError("Модель вернула пустую программу проверки.")

    questions = parse_program_questions(body)
    if not questions:
        questions = parse_program_questions(
            "## Вопросы, подлежащие аудиту\n\n" + (body or "")
        )
    questions = fit_program_questions(questions, hi)
    name = parse_program_heading(body, "Название проверки") or state.inspection_name

    paths = artifact_paths(case_id, state.inspection_name, PROGRAM_SPEC)
    yield {
        "type": "status",
        "message": "Собираю Word с программой проверки…",
        "elapsed_ms": elapsed(),
    }
    _write_markdown(
        paths.md,
        inspection_name=name,
        keywords=state.keywords,
        case_id=case_id,
        body=body,
        sources=sources,
        questions=questions,
    )
    write_program_docx(
        paths.primary,
        inspection_name=name,
        keywords=state.keywords,
        case_id=case_id,
        body=body,
        sources=sources,
        questions=questions,
    )
    paths.sources.write_text(
        json.dumps(sources, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    meta = _save_program_meta(
        state,
        docx=paths.primary,
        md=paths.md,
        sources=sources,
        body=body,
        items_min=lo,
        items_max=hi,
        questions=questions,
    )
    meta["digest"] = _digest(questions)
    yield {"type": "result", **meta, "elapsed_ms": elapsed()}


async def build_program(
    case_id: str,
    force: bool = False,
    items_min: int | None = None,
    items_max: int | None = None,
    items: str | None = None,
) -> dict:
    return await event_result(
        build_program_events(
            case_id,
            force=force,
            items_min=items_min,
            items_max=items_max,
            items=items,
        ),
        "Программа проверки не собрана",
    )
