from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from time import time

from app.config import settings
from app.models import CaseState
from app.prompts import prompt
from app.services.brief_flow import collect_brief_sources, resolve_brief_file
from app.services.case_context import (
    document_catalog,
    existing_cards,
    format_npa_sources,
    optional_block,
    read_truncated_md,
)
from app.services.conclusion_docx import (
    default_section_iii_title,
    ensure_all_hypotheses,
    missing_hypothesis_rows,
    parse_conclusion_markdown,
    write_conclusion_docx,
)
from app.services.document_artifact import (
    ArtifactOutcome,
    ArtifactPaths,
    ArtifactSpec,
    ComposeNotice,
    artifact_paths,
    artifact_stale,
    artifact_status,
    case_stale_extra,
    complete_llm,
    event_result,
    resolve_artifact_file,
    run_llm_artifact_events,
    upstream_built_at,
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
from app.storage import store

CONCLUSION_SCHEMA = 4
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
    from app.filenames import safe_stem

    stem = safe_stem(inspection_name or "proverka")
    suffix = (ext or "docx").lstrip(".")
    cid = (case_id or "case")[:12]
    return f"{stem}_zakluchenie_{cid}_{int(time())}.{suffix}"


def resolve_conclusion_file(case_id: str, kind: str) -> Path | None:
    return resolve_artifact_file(case_id, CONCLUSION_SPEC, kind)


def _md_body(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("## "):
            return "\n".join(lines[i:]).strip()
    return text.strip()


def refresh_conclusion_docx(case_id: str) -> Path | None:
    """Re-render Word from stored markdown so spacing always matches current generator."""
    md_path = resolve_conclusion_file(case_id, "md")
    docx_path = resolve_conclusion_file(case_id, "docx")
    if not md_path or not md_path.exists():
        return docx_path
    state = store.get(case_id)
    name = (state.inspection_name or "").strip()
    hypotheses = selected_hypothesis_rows(state)
    font = (state.meta.get("conclusion") or {}).get("font") or DEFAULT_FONT
    report = parse_conclusion_markdown(
        _md_body(md_path),
        hypotheses=hypotheses,
        inspection_name=name,
    )
    if docx_path is None:
        docx_path = artifact_paths(case_id, name, CONCLUSION_SPEC).primary
    write_conclusion_docx(
        docx_path,
        inspection_name=name,
        case_id=case_id,
        opinion_body=load_opinion_body(case_id),
        report=report,
        font=font,
    )
    return docx_path


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


def _digest(report) -> list[str]:
    out: list[str] = []
    for section in report.sections:
        if section.kind != "observations":
            continue
        for obs in section.observations:
            title = (obs.title or "").strip()
            if title:
                out.append(f"- Наблюдение {obs.number}: {title} ({obs.materiality})")
            if len(out) >= 20:
                return out
    return out


def _conclusion_stale(state: CaseState, font: str) -> bool:
    selection = state.meta.get("hypotheses_selection") or {}
    return artifact_stale(
        state,
        CONCLUSION_SPEC,
        schema=CONCLUSION_SCHEMA,
        extra=case_stale_extra(
            state,
            font=font,
            selected_ns=list(selection.get("selected_ns") or [])
            + list(selection.get("extra_ns") or []),
            **upstream_built_at(
                state, "hypotheses", "opinion", "program", "brief", "total"
            ),
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
        f"# {CONCLUSION_DOC_TITLE}",
        "",
        f"**Название проверки:** {inspection_name}",
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


def _clip(text: str, limit: int) -> str:
    body = (text or "").strip()
    if len(body) > limit:
        return body[:limit] + "\n…"
    return body


def _hypothesis_numbers(hypotheses: list[dict[str, str]]) -> str:
    return ", ".join(str(row.get("n") or i) for i, row in enumerate(hypotheses, start=1))


def _observation_outline(hypotheses: list[dict[str, str]]) -> str:
    lines: list[str] = []
    for i, row in enumerate(hypotheses, start=1):
        n = row.get("n") or i
        hyp = (row.get("hypothesis") or "").strip()
        if len(hyp) > 180:
            hyp = hyp[:177].rstrip() + "…"
        plan = (row.get("plan_sections") or "").strip()
        extra = f" (программа: {plan})" if plan else ""
        origin = " (гипотеза аудитора)" if (row.get("origin") or "") == "auditor" else ""
        lines.append(f"3.{i} ← гипотеза {n}{origin}{extra}: {hyp}")
    return "\n".join(lines) or "—"


def _cards_budget(state: CaseState, *, total_limit: int = 7000, per: int = 1800) -> str:
    blocks: list[str] = []
    used = 0
    for item in state.knowledge:
        if item.summary_status != "ok" or not (item.summary or "").strip():
            continue
        body = (item.summary or "").strip()
        if len(body) > per:
            body = body[:per] + "\n…"
        chunk = f"# {item.title}\n{body}"
        remain = total_limit - used
        if remain < 400:
            break
        if len(chunk) > remain:
            chunk = chunk[:remain] + "\n…"
        blocks.append(chunk)
        used += len(chunk)
        if used >= total_limit:
            break
    return "\n\n".join(blocks)


def _conclusion_num_predict(n: int) -> int:
    return min(24576, 4000 + max(n, 1) * 2800)


def _has_general_section(body: str) -> bool:
    return "общая информация об аудиторской проверке" in (body or "").lower()


async def _compose_conclusion(
    state: CaseState,
    *,
    hypotheses: list[dict[str, str]],
    sources: list[dict],
    opinion_body: str,
) -> str:
    count = len(hypotheses)
    section_iii_title = default_section_iii_title(state.inspection_name).rstrip(".")
    first_n = str(hypotheses[0].get("n") or "1") if hypotheses else "1"
    program_md = _clip(read_truncated_md(resolve_program_file, state.case_id, limit=16000), 12000)
    brief_md = _clip(read_truncated_md(resolve_brief_file, state.case_id, limit=16000), 9000)
    total_md = _clip(read_truncated_md(resolve_total_file, state.case_id, limit=8000), 2500 if brief_md else 4000)
    cards = _cards_budget(state) or existing_cards(state, limit=1800)
    opinion_trim = _clip(opinion_body, 3500)
    sections = prompt(
        "conclusion_sections",
        section_iii_title=section_iii_title,
        hypothesis_count=count,
        hypothesis_numbers=_hypothesis_numbers(hypotheses),
        first_hypothesis_n=first_n,
    ).strip()
    user = prompt(
        "conclusion_user",
        inspection=state.inspection_name,
        keywords=", ".join(state.keywords) or "не указаны",
        hypothesis_count=count,
        hypothesis_numbers=_hypothesis_numbers(hypotheses),
        observation_outline=_observation_outline(hypotheses),
        document_catalog=document_catalog(state),
        hypotheses_block=format_hypotheses_block(hypotheses),
        opinion_block=optional_block(
            "Раздел I (уже собран, не копировать)",
            opinion_trim,
            "Раздел I ещё не собран.",
        ),
        program_block=optional_block(
            "Программа проверки (черновик) — покрой все пункты",
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
        sections=sections,
    )
    return await chat_complete(
        prompt("conclusion_system"),
        user,
        timeout=settings.brief_timeout_sec,
        num_ctx=settings.ollama_num_ctx,
        num_predict=_conclusion_num_predict(count),
        temperature=0.2,
    )


async def _compose_remaining_observations(
    state: CaseState,
    *,
    hypotheses: list[dict[str, str]],
    missing: list[dict[str, str]],
    sources: list[dict],
    body: str,
) -> str:
    start = len(hypotheses) - len(missing) + 1
    next_number = f"3.{max(start, 1)}"
    general_tail = (
        "."
        if _has_general_section(body)
        else (
            " напиши раздел IV «Общая информация об аудиторской проверке» "
            "с полями из канона (основание, срок, группа, вид, дата)."
        )
    )
    program_md = _clip(read_truncated_md(resolve_program_file, state.case_id, limit=16000), 9000)
    brief_md = _clip(read_truncated_md(resolve_brief_file, state.case_id, limit=12000), 6000)
    cards = _cards_budget(state, total_limit=5000, per=1400)
    done = sorted(
        {
            str(row.get("n"))
            for row in hypotheses
            if str(row.get("n")) not in {str(item.get("n")) for item in missing}
        }
    )
    user = prompt(
        "conclusion_continue_user",
        inspection=state.inspection_name,
        keywords=", ".join(state.keywords) or "не указаны",
        done_list=", ".join(done) or "нет",
        hypothesis_count=len(missing),
        hypothesis_numbers=_hypothesis_numbers(missing),
        next_number=next_number,
        hypotheses_block=format_hypotheses_block(missing),
        program_block=optional_block(
            "Программа проверки",
            program_md,
            "Программа проверки ещё не собрана.",
        ),
        brief_block=optional_block("Саммари по актам", brief_md, "Саммари ещё не собрано."),
        cards_block=optional_block("Карточки актов", cards, "Карточки ещё не собраны."),
        fragments=format_npa_sources(sources, limit=18),
        general_tail=general_tail,
    )
    return await chat_complete(
        prompt("conclusion_system"),
        user,
        timeout=settings.brief_timeout_sec,
        num_ctx=settings.ollama_num_ctx,
        num_predict=_conclusion_num_predict(len(missing)),
        temperature=0.2,
    )


def _require_conclusion_inputs(state: CaseState) -> None:
    if not selected_hypothesis_rows(state):
        raise ValueError(
            "Сначала подтвердите гипотезы, которые войдут в заключение: "
            "`утверждаю гипотезы 1, 3, 5` или "
            "`утверждаю гипотезы все с приоритетом высокий`. "
            "Свои гипотезы — `утверждаю гипотезы 1, 2 плюс формулировка` или приложите Excel. "
            "Если чеклиста ещё нет — напишите `гипотезы`."
        )
    if not load_opinion_body(state.case_id):
        raise ValueError(
            "Сначала соберите раздел I: `аудиторское мнение` "
            "(`-c` Calibri или `-t` Times New Roman). "
            "Текст мнения войдёт в заключение как есть."
        )


def _conclusion_prepare(
    state: CaseState,
) -> tuple[list[dict[str, str]], list[dict], str]:
    return (
        selected_hypothesis_rows(state),
        collect_brief_sources(state),
        load_opinion_body(state.case_id),
    )


def _persist_conclusion(
    state: CaseState,
    paths: ArtifactPaths,
    body: str,
    ctx: tuple[list[dict[str, str]], list[dict], str],
    *,
    font: str,
) -> ArtifactOutcome:
    hypotheses, sources, opinion_body = ctx
    name = (state.inspection_name or "").strip()
    report = parse_conclusion_markdown(
        body,
        hypotheses=hypotheses,
        inspection_name=name,
    )
    report = ensure_all_hypotheses(report, hypotheses, inspection_name=name)
    _write_markdown(
        paths.md,
        inspection_name=name,
        keywords=state.keywords,
        case_id=state.case_id,
        body=body,
        font=font,
        hypotheses=hypotheses,
    )
    write_conclusion_docx(
        paths.primary,
        inspection_name=name,
        case_id=state.case_id,
        opinion_body=opinion_body,
        report=report,
        font=font,
    )
    selection = state.meta.get("hypotheses_selection") or {}
    return ArtifactOutcome(
        body=body,
        sources=sources,
        extra={
            "schema": CONCLUSION_SCHEMA,
            "font": font,
            "keywords": list(state.keywords),
            "selected_ns": [int(row["n"]) for row in hypotheses],
            **upstream_built_at(
                state, "hypotheses", "opinion", "program", "brief", "total"
            ),
            "selection_at": selection.get("selected_at"),
        },
        digest=_digest(report),
        sources_file={
            "font": font,
            "selected_ns": [int(row["n"]) for row in hypotheses],
            "sources": sources,
        },
    )


async def _compose_conclusion_stream(
    state: CaseState,
    ctx: tuple[list[dict[str, str]], list[dict], str],
):
    hypotheses, sources, opinion_body = ctx
    body = await complete_llm(
        _compose_conclusion(
            state,
            hypotheses=hypotheses,
            sources=sources,
            opinion_body=opinion_body,
        ),
        fail="Модель не собрала аудиторское заключение",
        empty="Модель вернула пустое аудиторское заключение.",
    )
    name = (state.inspection_name or "").strip()
    report = parse_conclusion_markdown(
        body,
        hypotheses=hypotheses,
        inspection_name=name,
    )
    missing = missing_hypothesis_rows(report, hypotheses)
    attempts = 0
    while missing and attempts < 2:
        ns = ", ".join(str(row.get("n")) for row in missing)
        yield ComposeNotice(f"Дописываю недостающие наблюдения по гипотезам {ns}…")
        extra = await complete_llm(
            _compose_remaining_observations(
                state,
                hypotheses=hypotheses,
                missing=missing,
                sources=sources,
                body=body,
            ),
            fail="Модель не дописала наблюдения заключения",
        )
        if (extra or "").strip():
            body = (body or "").rstrip() + "\n\n" + extra.strip()
            report = parse_conclusion_markdown(
                body,
                hypotheses=hypotheses,
                inspection_name=name,
            )
            missing = missing_hypothesis_rows(report, hypotheses)
        else:
            break
        attempts += 1
    yield body


async def build_conclusion_events(
    case_id: str,
    force: bool = False,
    font: str | None = None,
) -> AsyncIterator[dict]:
    resolved_font = parse_document_font(font) if font else DEFAULT_FONT

    async for event in run_llm_artifact_events(
        case_id,
        CONCLUSION_SPEC,
        force=force,
        start_message="Собираю подтверждённые гипотезы, мнение и материалы проверки…",
        already_message="Аудиторское заключение уже собрано — отдаю файл.",
        prepare_message="Отбираю фрагменты из приложенных документов…",
        compose_message=lambda _state, ctx: (
            f"Пишу черновик аудиторского заключения ({resolved_font}): "
            f"{len(ctx[0])} наблюдений по подтверждённым гипотезам. "
            "Это может занять несколько минут…"
        ),
        writing_message="Собираю Word с аудиторским заключением…",
        load_state=ingest_library,
        inspect=_require_conclusion_inputs,
        is_stale=lambda state: _conclusion_stale(state, resolved_font),
        prepare=_conclusion_prepare,
        compose=_compose_conclusion_stream,
        write=lambda state, paths, body, ctx: _persist_conclusion(
            state, paths, body, ctx, font=resolved_font
        ),
        compose_fail="Модель не собрала аудиторское заключение",
        empty_error="Модель вернула пустое аудиторское заключение.",
    ):
        yield event


async def build_conclusion(
    case_id: str,
    force: bool = False,
    font: str | None = None,
) -> dict:
    return await event_result(
        build_conclusion_events(case_id, force=force, font=font),
        "Аудиторское заключение не собрано",
    )
