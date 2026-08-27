from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

from app.config import settings
from app.models import CaseState
from app.prompts import prompt
from app.services.brief_flow import collect_brief_sources, resolve_brief_file
from app.services.case_context import (
    document_catalog,
    existing_cards,
    format_npa_sources,
    read_truncated_md,
)
from app.services.conclusion_docx import parse_conclusion_markdown, write_conclusion_docx
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
from app.services.hypotheses_flow import selected_hypothesis_rows
from app.services.knowledge_ingest import ingest_library
from app.services.ollama_client import chat_complete
from app.services.opinion_flow import (
    DEFAULT_FONT,
    format_hypotheses_block,
    parse_document_font,
    resolve_opinion_file,
)
from app.services.program_flow import resolve_program_file
from app.services.total_flow import resolve_total_file

CONCLUSION_SCHEMA = 2
CONCLUSION_SPEC = ArtifactSpec(
    meta_key="conclusion",
    directory="reports",
    file_prefix="zakluchenie",
    md_name="conclusion.md",
    sources_name="conclusion_sources.json",
    download_suffix="zakluchenie",
    docx_endpoint="/api/v1/cases/{case_id}/knowledge/conclusion.docx",
    md_endpoint="/api/v1/cases/{case_id}/knowledge/conclusion.md",
    docx_glob="zakluchenie_*.docx",
)
CONCLUSION_DOC_TITLE = "Аудиторское заключение (черновик)"


def conclusion_download_name(inspection_name: str, case_id: str = "", ext: str = "docx") -> str:
    _ = case_id
    return artifact_download_name(inspection_name, CONCLUSION_SPEC, ext=ext)


def resolve_conclusion_file(case_id: str, kind: str) -> Path | None:
    return resolve_artifact_file(case_id, CONCLUSION_SPEC, kind)


def conclusion_status(case_id: str) -> dict:
    return artifact_status(case_id, CONCLUSION_SPEC)


def load_opinion_body(case_id: str) -> str:
    path = resolve_opinion_file(case_id, "md")
    if not path or not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("## "):
            return "\n".join(lines[i:]).strip()
    return text.strip()


def _optional_block(label: str, text: str, empty: str) -> str:
    body = (text or "").strip()
    if body:
        return f"{label}:\n{body}\n"
    return f"{empty}\n"


def _digest(report) -> list[str]:
    out: list[str] = []
    for section in report.sections:
        if section.kind != "observations":
            continue
        for obs in section.observations:
            title = (obs.title or "").strip()
            if title:
                out.append(f"- Наблюдение {obs.number}: {title} ({obs.materiality})")
            if len(out) >= 8:
                return out
    return out


def _conclusion_stale(state: CaseState, font: str) -> bool:
    selection = state.meta.get("hypotheses_selection") or {}
    return artifact_stale(
        state,
        CONCLUSION_SPEC,
        schema=CONCLUSION_SCHEMA,
        extra={
            "keywords": list(state.keywords),
            "inspection_name": state.inspection_name,
            "font": font,
            "selected_ns": list(selection.get("selected_ns") or []),
            "hypotheses_built_at": (state.meta.get("hypotheses") or {}).get("built_at"),
            "opinion_built_at": (state.meta.get("opinion") or {}).get("built_at"),
            "program_built_at": (state.meta.get("program") or {}).get("built_at"),
            "brief_built_at": (state.meta.get("brief") or {}).get("built_at"),
            "total_built_at": (state.meta.get("total") or {}).get("built_at"),
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
    font: str,
    hypotheses: list[dict[str, str]],
) -> None:
    ns = ", ".join(str(row.get("n")) for row in hypotheses) or "—"
    lines = [
        f"# {CONCLUSION_DOC_TITLE}",
        "",
        f"**Название проверки:** {inspection_name}",
        f"**Аудируемый период:** {period or 'уточняется'}",
        f"**Шрифт:** {font}",
        f"Ключевые слова: {', '.join(keywords) or '—'}. Кейс `{case_id}`.",
        f"Подтверждённые гипотезы: {ns}.",
        "",
        "Черновик аудиторского заключения. Раздел II не генерируется.",
        "",
        (body or "").strip(),
        "",
    ]
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def _save_conclusion_meta(
    state: CaseState,
    *,
    docx: Path,
    md: Path,
    body: str,
    font: str,
    hypotheses: list[dict[str, str]],
    sources: list[dict],
) -> dict:
    selection = state.meta.get("hypotheses_selection") or {}
    return save_artifact_meta(
        state,
        CONCLUSION_SPEC,
        docx=docx,
        md=md,
        sources=sources,
        body=body,
        extra={
            "schema": CONCLUSION_SCHEMA,
            "font": font,
            "keywords": list(state.keywords),
            "selected_ns": [int(row["n"]) for row in hypotheses],
            "hypotheses_built_at": (state.meta.get("hypotheses") or {}).get("built_at"),
            "opinion_built_at": (state.meta.get("opinion") or {}).get("built_at"),
            "program_built_at": (state.meta.get("program") or {}).get("built_at"),
            "brief_built_at": (state.meta.get("brief") or {}).get("built_at"),
            "total_built_at": (state.meta.get("total") or {}).get("built_at"),
            "selection_at": selection.get("selected_at"),
        },
    )


async def _compose_conclusion(
    state: CaseState,
    *,
    hypotheses: list[dict[str, str]],
    sources: list[dict],
    opinion_body: str,
) -> str:
    program_md = read_truncated_md(resolve_program_file, state.case_id, limit=6000)
    brief_md = read_truncated_md(resolve_brief_file, state.case_id, limit=4000)
    total_md = read_truncated_md(resolve_total_file, state.case_id, limit=4000)
    cards = existing_cards(state, limit=3000)
    opinion_trim = (opinion_body or "").strip()
    if len(opinion_trim) > 5000:
        opinion_trim = opinion_trim[:5000] + "\n…"
    user = prompt(
        "conclusion_user",
        inspection=state.inspection_name,
        keywords=", ".join(state.keywords) or "не указаны",
        period=state.period or "не указан",
        document_catalog=document_catalog(state),
        hypotheses_block=format_hypotheses_block(hypotheses),
        opinion_block=_optional_block(
            "Раздел I (уже собран, не копировать)",
            opinion_trim,
            "Раздел I ещё не собран.",
        ),
        program_block=_optional_block(
            "Программа проверки (черновик)",
            program_md,
            "Программа проверки ещё не собрана.",
        ),
        brief_block=_optional_block(
            "Саммари по актам",
            brief_md,
            "Саммари по базе знаний ещё не собрано.",
        ),
        total_block=_optional_block(
            "Саммари total",
            total_md,
            "Саммари total ещё не собрано.",
        ),
        cards_block=_optional_block(
            "Карточки актов",
            cards,
            "Карточки саммари ещё не собраны.",
        ),
        fragments=format_npa_sources(sources, limit=20),
        sections=prompt("conclusion_sections").strip(),
    )
    return await chat_complete(
        prompt("conclusion_system"),
        user,
        timeout=settings.brief_timeout_sec,
        num_ctx=settings.ollama_num_ctx,
        num_predict=8192,
        temperature=0.2,
    )


async def build_conclusion_events(
    case_id: str,
    force: bool = False,
    font: str | None = None,
) -> AsyncIterator[dict]:
    timer = ElapsedTimer()
    elapsed = timer.ms
    resolved_font = parse_document_font(font) if font else DEFAULT_FONT

    yield {
        "type": "status",
        "message": "Собираю подтверждённые гипотезы, мнение и материалы проверки…",
        "elapsed_ms": elapsed(),
    }
    state = ingest_library(case_id)
    hypotheses = selected_hypothesis_rows(state)
    if not hypotheses:
        raise ValueError(
            "Сначала подтвердите гипотезы, которые войдут в заключение: "
            "`утверждаю гипотезы 1, 3, 5` или "
            "`утверждаю гипотезы все с приоритетом высокий`. "
            "Если чеклиста ещё нет — напишите `гипотезы`."
        )
    opinion_body = load_opinion_body(case_id)
    if not opinion_body:
        raise ValueError(
            "Сначала соберите раздел I: `аудиторское мнение` "
            "(`-c` Calibri или `-t` Times New Roman). "
            "Текст мнения войдёт в заключение как есть."
        )

    if not force and not _conclusion_stale(state, resolved_font):
        meta = conclusion_status(case_id)
        yield {
            "type": "status",
            "message": "Аудиторское заключение уже собрано — отдаю файл.",
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
        "message": (
            f"Пишу черновик аудиторского заключения ({resolved_font}). "
            "Это может занять несколько минут…"
        ),
        "elapsed_ms": elapsed(),
    }
    try:
        body = await _compose_conclusion(
            state,
            hypotheses=hypotheses,
            sources=sources,
            opinion_body=opinion_body,
        )
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Модель не собрала аудиторское заключение: {exc}") from exc
    if not (body or "").strip():
        raise ValueError("Модель вернула пустое аудиторское заключение.")

    report = parse_conclusion_markdown(
        body,
        hypotheses=hypotheses,
        period=state.period,
    )
    name = (state.inspection_name or "").strip()
    period = state.period
    paths = artifact_paths(case_id, name, CONCLUSION_SPEC)
    yield {
        "type": "status",
        "message": "Собираю Word с аудиторским заключением…",
        "elapsed_ms": elapsed(),
    }
    _write_markdown(
        paths.md,
        inspection_name=name,
        period=period,
        keywords=state.keywords,
        case_id=case_id,
        body=body,
        font=resolved_font,
        hypotheses=hypotheses,
    )
    write_conclusion_docx(
        paths.primary,
        inspection_name=name,
        period=period,
        case_id=case_id,
        opinion_body=opinion_body,
        report=report,
        font=resolved_font,
    )
    paths.sources.write_text(
        json.dumps(
            {
                "font": resolved_font,
                "selected_ns": [int(row["n"]) for row in hypotheses],
                "sources": sources,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    meta = _save_conclusion_meta(
        state,
        docx=paths.primary,
        md=paths.md,
        body=body,
        font=resolved_font,
        hypotheses=hypotheses,
        sources=sources,
    )
    meta["digest"] = _digest(report)
    yield {"type": "result", **meta, "elapsed_ms": elapsed()}


async def build_conclusion(
    case_id: str,
    force: bool = False,
    font: str | None = None,
) -> dict:
    return await event_result(
        build_conclusion_events(case_id, force=force, font=font),
        "Аудиторское заключение не собрано",
    )
