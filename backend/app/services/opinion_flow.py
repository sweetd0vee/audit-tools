from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path

from app.config import settings
from app.models import CaseState
from app.prompts import prompt
from app.services.brief_docx import write_opinion_docx
from app.services.brief_flow import collect_brief_sources, resolve_brief_file
from app.services.case_context import (
    document_catalog,
    existing_cards,
    format_npa_sources,
    optional_block,
    read_truncated_md,
)
from app.services.document_artifact import (
    ArtifactOutcome,
    ArtifactPaths,
    ArtifactSpec,
    artifact_download_name,
    artifact_stale,
    artifact_status,
    case_stale_extra,
    event_result,
    resolve_artifact_file,
    run_llm_artifact_events,
    upstream_built_at,
)
from app.services.hypotheses_flow import selected_hypothesis_rows
from app.services.knowledge_ingest import ingest_library
from app.services.ollama_client import chat_complete
from app.services.program_flow import resolve_program_file
from app.services.total_flow import resolve_total_file

OPINION_SCHEMA = 2
OPINION_SPEC = ArtifactSpec(
    meta_key="opinion",
    directory="opinions",
    file_prefix="mnenie",
    md_name="opinion.md",
    sources_name="opinion_sources.json",
    download_suffix="mnenie",
    docx_endpoint="/api/v1/cases/{case_id}/knowledge/opinion.docx",
    md_endpoint="/api/v1/cases/{case_id}/knowledge/opinion.md",
    docx_glob="mnenie_*.docx",
)

FONT_CALIBRI = "Calibri"
FONT_TIMES = "Times New Roman"
DEFAULT_FONT = FONT_TIMES
OPINION_DOC_TITLE = "I. Аудиторское мнение по итогам проверки"


def parse_document_font(value: str | None) -> str:
    raw = (value or "").strip().lower()
    if not raw:
        return DEFAULT_FONT
    compact = re.sub(r"[\s_\-]+", "", raw)
    if compact in {"c", "calibri", "калибри"}:
        return FONT_CALIBRI
    if compact in {"t", "times", "timesnewroman", "timesnew", "таймс", "таймсниурома"}:
        return FONT_TIMES
    if "calibri" in raw or "калибри" in raw:
        return FONT_CALIBRI
    if "times" in raw or "таймс" in raw:
        return FONT_TIMES
    return DEFAULT_FONT


def parse_opinion_font_flag(text: str) -> str:
    cleaned = re.sub(r"заново|пересобер\w*|перегенер\w*|force", " ", text or "", flags=re.I)
    if re.search(r"(?:^|\s)-c(?:\s|$)|(?<!\w)calibri(?!\w)|калибри", cleaned, re.I):
        return FONT_CALIBRI
    if re.search(
        r"(?:^|\s)-t(?:\s|$)|times(?:\s+new\s+roman)?|таймс",
        cleaned,
        re.I,
    ):
        return FONT_TIMES
    return DEFAULT_FONT


def opinion_download_name(inspection_name: str, case_id: str = "", ext: str = "docx") -> str:
    _ = case_id
    return artifact_download_name(inspection_name, OPINION_SPEC, ext=ext)


def resolve_opinion_file(case_id: str, kind: str) -> Path | None:
    return resolve_artifact_file(case_id, OPINION_SPEC, kind)


def opinion_status(case_id: str) -> dict:
    return artifact_status(case_id, OPINION_SPEC)


def format_hypotheses_block(rows: list[dict[str, str]]) -> str:
    lines: list[str] = []
    for row in rows:
        n = row.get("n") or "?"
        priority = row.get("priority") or "средний"
        origin = (row.get("origin") or "").strip()
        tag = " [гипотеза аудитора]" if origin == "auditor" else ""
        lines.append(f"{n}. [{priority}] {row.get('hypothesis') or ''}{tag}")
        for label, key in (
            ("Утверждение", "assertion"),
            ("Риск", "risk"),
            ("Почему риск", "why_risk"),
            ("Как проверяли", "how_to_test"),
            ("Что запросить", "evidence_request"),
            ("Рабочий документ", "working_paper"),
            ("НПА / критерии", "npa_criteria"),
            ("Разделы плана", "plan_sections"),
        ):
            value = (row.get(key) or "").strip()
            if value:
                lines.append(f"   {label}: {value}")
        lines.append("")
    return "\n".join(lines).strip() or "Подтверждённых гипотез нет."


def _digest(body: str, limit: int = 8) -> list[str]:
    out: list[str] = []
    capturing = False
    for line in (body or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            capturing = "рекомендац" in stripped.lower()
            continue
        if capturing and (stripped.startswith("- ") or stripped.startswith("* ")):
            item = stripped[2:].strip()
            if item:
                out.append(f"- {item}")
            if len(out) >= limit:
                return out
    if out:
        return out
    for line in (body or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") or stripped.startswith("* "):
            item = stripped[2:].strip()
            if item:
                out.append(f"- {item}")
            if len(out) >= limit:
                break
    return out


def _opinion_stale(state: CaseState, font: str) -> bool:
    selection = state.meta.get("hypotheses_selection") or {}
    return artifact_stale(
        state,
        OPINION_SPEC,
        schema=OPINION_SCHEMA,
        extra=case_stale_extra(
            state,
            font=font,
            selected_ns=list(selection.get("selected_ns") or [])
            + list(selection.get("extra_ns") or []),
            **upstream_built_at(state, "hypotheses", "program", "brief", "total"),
        ),
    )


def _write_markdown(
    path: Path,
    *,
    inspection_name: str,
    keywords: list[str],
    case_id: str,
    body: str,
    font: str,
    hypotheses: list[dict[str, str]],
) -> None:
    ns = ", ".join(str(row.get("n")) for row in hypotheses) or "—"
    lines = [
        f"# {OPINION_DOC_TITLE}",
        "",
        f"**Название проверки:** {inspection_name}",
        f"**Шрифт:** {font}",
        f"Ключевые слова: {', '.join(keywords) or '—'}. Кейс `{case_id}`.",
        f"Подтверждённые гипотезы: {ns}.",
        "",
        "Черновик раздела I аудиторского заключения. Без таблиц и схем.",
        "",
        (body or "").strip(),
        "",
    ]
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def _require_selected_hypotheses(state: CaseState) -> None:
    if selected_hypothesis_rows(state):
        return
    raise ValueError(
        "Сначала подтвердите гипотезы, которые войдут в мнение: "
        "`утверждаю гипотезы 1, 3, 5` или "
        "`утверждаю гипотезы все с приоритетом высокий`. "
        "Свои гипотезы — приложите Excel к этой же команде. "
        "Если чеклиста ещё нет — напишите `гипотезы`."
    )


def _opinion_prepare(state: CaseState) -> tuple[list[dict[str, str]], list[dict]]:
    return selected_hypothesis_rows(state), collect_brief_sources(state)


def _persist_opinion(
    state: CaseState,
    paths: ArtifactPaths,
    body: str,
    ctx: tuple[list[dict[str, str]], list[dict]],
    *,
    font: str,
) -> ArtifactOutcome:
    hypotheses, sources = ctx
    name = (state.inspection_name or "").strip()
    _write_markdown(
        paths.md,
        inspection_name=name,
        keywords=state.keywords,
        case_id=state.case_id,
        body=body,
        font=font,
        hypotheses=hypotheses,
    )
    write_opinion_docx(
        paths.primary,
        inspection_name=name,
        keywords=state.keywords,
        case_id=state.case_id,
        body=body,
        font=font,
    )
    selection = state.meta.get("hypotheses_selection") or {}
    return ArtifactOutcome(
        body=body,
        sources=sources,
        extra={
            "schema": OPINION_SCHEMA,
            "font": font,
            "keywords": list(state.keywords),
            "selected_ns": [int(row["n"]) for row in hypotheses],
            **upstream_built_at(state, "hypotheses", "program", "brief", "total"),
            "selection_at": selection.get("selected_at"),
        },
        digest=_digest(body),
        sources_file={
            "font": font,
            "selected_ns": [int(row["n"]) for row in hypotheses],
            "sources": sources,
        },
    )


async def build_opinion_events(
    case_id: str,
    force: bool = False,
    font: str | None = None,
) -> AsyncIterator[dict]:
    resolved_font = parse_document_font(font)

    async def compose(state: CaseState, ctx: tuple[list[dict[str, str]], list[dict]]) -> str:
        hypotheses, sources = ctx
        return await _compose_opinion(state, hypotheses=hypotheses, sources=sources)

    async for event in run_llm_artifact_events(
        case_id,
        OPINION_SPEC,
        force=force,
        start_message="Собираю подтверждённые гипотезы и материалы проверки…",
        already_message="Аудиторское мнение уже собрано — отдаю файл.",
        prepare_message="Отбираю фрагменты из приложенных документов…",
        compose_message=(
            f"Пишу раздел I аудиторского заключения ({resolved_font}). "
            "Это может занять несколько минут…"
        ),
        writing_message="Собираю Word с аудиторским мнением…",
        load_state=ingest_library,
        inspect=_require_selected_hypotheses,
        is_stale=lambda state: _opinion_stale(state, resolved_font),
        prepare=_opinion_prepare,
        compose=compose,
        write=lambda state, paths, body, ctx: _persist_opinion(
            state, paths, body, ctx, font=resolved_font
        ),
        compose_fail="Модель не собрала аудиторское мнение",
        empty_error="Модель вернула пустое аудиторское мнение.",
    ):
        yield event


async def _compose_opinion(
    state: CaseState,
    *,
    hypotheses: list[dict[str, str]],
    sources: list[dict],
) -> str:
    program_md = read_truncated_md(resolve_program_file, state.case_id, limit=8000)
    brief_md = read_truncated_md(resolve_brief_file, state.case_id, limit=6000)
    total_md = read_truncated_md(resolve_total_file, state.case_id, limit=6000)
    cards = existing_cards(state, limit=4000)
    user = prompt(
        "opinion_user",
        inspection=state.inspection_name,
        keywords=", ".join(state.keywords) or "не указаны",
        document_catalog=document_catalog(state),
        hypotheses_block=format_hypotheses_block(hypotheses),
        program_block=optional_block(
            "Программа проверки (черновик)",
            program_md,
            "Программа проверки ещё не собрана.",
        ),
        brief_block=optional_block(
            "Саммари по актам",
            brief_md,
            "Саммари по базе знаний ещё не собрано.",
        ),
        total_block=optional_block(
            "Саммари total",
            total_md,
            "Саммари total ещё не собрано.",
        ),
        cards_block=optional_block(
            "Карточки актов",
            cards,
            "Карточки саммари ещё не собраны.",
        ),
        fragments=format_npa_sources(sources, limit=24),
        sections=prompt("opinion_sections").strip(),
        target=3600,
        target_hi=7200,
    )
    return await chat_complete(
        prompt("opinion_system"),
        user,
        timeout=settings.brief_timeout_sec,
        num_ctx=settings.ollama_num_ctx,
        num_predict=4096,
        temperature=0.2,
    )


async def build_opinion(
    case_id: str,
    force: bool = False,
    font: str | None = None,
) -> dict:
    return await event_result(
        build_opinion_events(case_id, force=force, font=font),
        "Аудиторское мнение не собрано",
    )
