from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from app.clock import utc_now
from app.config import settings
from app.models import CaseState
from app.prompts import prompt
from app.services.brief_flow import collect_brief_sources
from app.services.case_context import (
    append_npa_sources_markdown,
    document_catalog,
    existing_cards,
    format_npa_sources,
    read_truncated_md,
)
from app.services.document_artifact import (
    ArtifactOutcome,
    ArtifactPaths,
    ArtifactSpec,
    artifact_dir,
    artifact_download_name,
    artifact_stale,
    artifact_status,
    case_stale_extra,
    event_result,
    knowledge_ok_count,
    resolve_artifact_file,
    run_llm_artifact_events,
    upstream_built_at,
)
from app.services.hypotheses_xlsx import read_hypotheses_xlsx, write_hypotheses_xlsx
from app.services.knowledge_ingest import ingest_library
from app.services.ollama_client import chat_complete, extract_json_value
from app.services.program_flow import resolve_program_file
from app.services.total_flow import resolve_total_file
from app.storage import store

HYPOTHESES_SCHEMA = 2
EXTRA_HYPOTHESES_MAX = 20
AUDITOR_ORIGIN = "auditor"
AUDITOR_BASIS = "гипотеза аудитора — из приложенного Excel"
HYPOTHESES_SPEC = ArtifactSpec(
    meta_key="hypotheses",
    directory="hypotheses",
    file_prefix="gipotezy",
    md_name="hypotheses.md",
    sources_name="hypotheses_sources.json",
    download_suffix="gipotezy",
    docx_endpoint="/api/v1/cases/{case_id}/knowledge/hypotheses.xlsx",
    md_endpoint="/api/v1/cases/{case_id}/knowledge/hypotheses.md",
    docx_glob="gipotezy_*.xlsx",
    primary_ext="xlsx",
)

_ROW_FIELDS = (
    "hypothesis",
    "assertion",
    "risk",
    "plan_sections",
    "npa_criteria",
    "why_risk",
    "how_to_test",
    "evidence_request",
    "working_paper",
    "priority",
    "basis",
)

_PRIORITY_MAP = {
    "высокий": "высокий",
    "высокая": "высокий",
    "high": "высокий",
    "средний": "средний",
    "средняя": "средний",
    "medium": "средний",
    "низкий": "низкий",
    "низкая": "низкий",
    "low": "низкий",
}


def _json_path(case_id: str) -> Path:
    return artifact_dir(case_id, HYPOTHESES_SPEC) / "hypotheses.json"


def _extra_json_path(case_id: str) -> Path:
    return artifact_dir(case_id, HYPOTHESES_SPEC) / "extra_hypotheses.json"


def _extra_xlsx_path(case_id: str) -> Path:
    return artifact_dir(case_id, HYPOTHESES_SPEC) / "auditor_gipotezy.xlsx"


def _norm_hypothesis_text(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


def hypotheses_download_name(
    inspection_name: str, case_id: str = "", ext: str = "xlsx"
) -> str:
    _ = case_id
    return artifact_download_name(inspection_name, HYPOTHESES_SPEC, ext=ext)


def resolve_hypotheses_file(case_id: str, kind: str) -> Path | None:
    if kind == "json":
        path = _json_path(case_id)
        return path if path.exists() else None
    return resolve_artifact_file(case_id, HYPOTHESES_SPEC, kind)


def hypotheses_status(case_id: str) -> dict:
    return artifact_status(case_id, HYPOTHESES_SPEC)


def _hypotheses_stale(state: CaseState) -> bool:
    return artifact_stale(
        state,
        HYPOTHESES_SPEC,
        schema=HYPOTHESES_SCHEMA,
        check_items=True,
        extra=case_stale_extra(
            state,
            **upstream_built_at(state, "brief", "total", "program"),
        ),
    )


def _normalize_priority(value: str) -> str:
    key = (value or "").strip().lower()
    return _PRIORITY_MAP.get(key, "средний")


def _sort_rows_by_priority(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    order = {"высокий": 0, "средний": 1, "низкий": 2}
    ranked = sorted(rows, key=lambda r: order.get(r.get("priority") or "", 1))
    for i, row in enumerate(ranked, start=1):
        row["n"] = str(i)
    return ranked


def _normalize_row(raw: dict[str, Any], index: int) -> dict[str, str]:
    row: dict[str, str] = {"n": str(index)}
    for field in _ROW_FIELDS:
        value = raw.get(field)
        if value is None and field == "hypothesis":
            value = raw.get("гипотеза") or raw.get("title")
        row[field] = str(value or "").strip()
    if not row["hypothesis"]:
        raise ValueError(f"Гипотеза #{index}: пустое поле hypothesis")
    for field in _ROW_FIELDS:
        if not row[field]:
            if field == "priority":
                row[field] = "средний"
            elif field == "basis":
                row[field] = "материалы кейса / знания модели — уточнить по первоисточнику"
            else:
                row[field] = "уточнить при планировании"
    row["priority"] = _normalize_priority(row["priority"])
    return row


def parse_hypotheses_payload(raw: str | dict | list) -> tuple[list[dict[str, str]], str]:
    if isinstance(raw, str):
        parsed = extract_json_value(raw)
    else:
        parsed = raw

    notes = ""
    items: list[Any]
    if isinstance(parsed, dict):
        notes = str(parsed.get("notes") or "").strip()
        items = parsed.get("hypotheses") or parsed.get("items") or parsed.get("rows") or []
    elif isinstance(parsed, list):
        items = parsed
    else:
        raise ValueError("Модель вернула неожиданный JSON для гипотез")

    if not isinstance(items, list) or not items:
        raise ValueError("Модель не вернула список гипотез")

    rows = [_normalize_row(item, i) for i, item in enumerate(items, start=1) if isinstance(item, dict)]
    if len(rows) < 8:
        raise ValueError(f"Нужно 8–10 гипотез, модель вернула {len(rows)}")
    if len(rows) > 10:
        rows = rows[:10]
    rows = _sort_rows_by_priority(rows)
    return rows, notes


def load_hypotheses_rows(case_id: str) -> tuple[list[dict[str, str]], str]:
    path = _json_path(case_id)
    if not path.exists():
        raise ValueError("Чеклист гипотез ещё не собран. Напишите `гипотезы`.")
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("hypotheses") or []
    notes = str(payload.get("notes") or "")
    if not isinstance(rows, list) or not rows:
        raise ValueError("В чеклисте гипотез нет строк. Соберите `гипотезы` заново.")
    clean = [row for row in rows if isinstance(row, dict)]
    if not clean:
        raise ValueError("В чеклисте гипотез нет строк. Соберите `гипотезы` заново.")
    return clean, notes


def load_extra_hypothesis_rows(case_id: str) -> list[dict[str, str]]:
    path = _extra_json_path(case_id)
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("hypotheses") or []
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict) and (row.get("hypothesis") or "").strip()]


def _clear_hypothesis_extras(case_id: str) -> None:
    for path in (_extra_json_path(case_id), _extra_xlsx_path(case_id)):
        if path.exists():
            path.unlink()


def _next_hypothesis_n(rows: list[dict[str, str]]) -> int:
    highest = 0
    for row in rows:
        raw = str(row.get("n") or "").strip()
        if raw.isdigit():
            highest = max(highest, int(raw))
    return highest + 1


def _normalize_auditor_row(raw: dict[str, Any], index: int) -> dict[str, str]:
    row = _normalize_row(raw, index)
    row["origin"] = AUDITOR_ORIGIN
    if not str(raw.get("basis") or "").strip():
        row["basis"] = AUDITOR_BASIS
    return row


def _dedupe_extra_rows(
    extras: list[dict[str, Any]],
    generated: list[dict[str, str]],
) -> list[dict[str, Any]]:
    known = {_norm_hypothesis_text(str(row.get("hypothesis") or "")) for row in generated}
    known.discard("")
    seen = set(known)
    out: list[dict[str, Any]] = []
    for raw in extras:
        if not isinstance(raw, dict):
            continue
        text = _norm_hypothesis_text(
            str(raw.get("hypothesis") or raw.get("гипотеза") or raw.get("title") or "")
        )
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(raw)
    return out


def parse_auditor_hypotheses(
    *,
    generated: list[dict[str, str]],
    extra_rows: list[dict[str, Any]] | None = None,
    extra_xlsx: bytes | None = None,
) -> list[dict[str, str]]:
    incoming: list[dict[str, Any]] = []
    if extra_xlsx:
        incoming.extend(read_hypotheses_xlsx(extra_xlsx))
    for raw in extra_rows or []:
        if isinstance(raw, dict):
            incoming.append(raw)
    incoming = _dedupe_extra_rows(incoming, generated)
    if extra_xlsx is not None or extra_rows:
        if not incoming:
            raise ValueError(
                "В файле нет новых гипотез — все строки совпадают с чеклистом. "
                "Допишите свои формулировки в колонку «Гипотеза»."
            )
    if len(incoming) > EXTRA_HYPOTHESES_MAX:
        raise ValueError(
            f"Слишком много своих гипотез: {len(incoming)}. "
            f"Максимум {EXTRA_HYPOTHESES_MAX}."
        )
    start = _next_hypothesis_n(generated)
    return [_normalize_auditor_row(item, start + i) for i, item in enumerate(incoming)]


def _store_extra_hypotheses(
    case_id: str,
    rows: list[dict[str, str]],
    extra_xlsx: bytes | None = None,
    extra_filename: str | None = None,
) -> None:
    if not rows:
        _clear_hypothesis_extras(case_id)
        return
    _extra_json_path(case_id).write_text(
        json.dumps(
            {
                "filename": extra_filename or "",
                "hypotheses": rows,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    if extra_xlsx:
        _extra_xlsx_path(case_id).write_bytes(extra_xlsx)


def resolve_hypothesis_selection(
    rows: list[dict[str, str]],
    *,
    numbers: list[int] | None = None,
    all_high: bool = False,
    all_rows: bool = False,
) -> list[int]:
    available = []
    for row in rows:
        raw = str(row.get("n") or "").strip()
        if raw.isdigit():
            available.append(int(raw))
    if not available:
        raise ValueError("В чеклисте гипотез нет номеров.")
    if all_rows:
        return available
    if all_high:
        picked = [
            int(row["n"])
            for row in rows
            if (row.get("priority") or "") == "высокий"
            and str(row.get("n") or "").strip().isdigit()
        ]
        if not picked:
            raise ValueError(
                "Нет гипотез с приоритетом «высокий». "
                "Укажите номера: `утверждаю гипотезы 1, 3, 5`."
            )
        return picked
    wanted: list[int] = []
    seen: set[int] = set()
    for n in numbers or []:
        if n in seen:
            continue
        seen.add(n)
        if n not in available:
            listed = ", ".join(str(x) for x in available)
            raise ValueError(f"Нет гипотезы №{n}. В чеклисте номера: {listed}.")
        wanted.append(n)
    if not wanted:
        raise ValueError(
            "Укажите номера гипотез, например: `утверждаю гипотезы 1, 3, 5` "
            "или `утверждаю гипотезы все с приоритетом высокий`."
        )
    return wanted


def select_hypotheses(
    case_id: str,
    *,
    numbers: list[int] | None = None,
    all_high: bool = False,
    all_rows: bool = False,
    keep_numbers: bool = False,
    extra_xlsx: bytes | None = None,
    extra_filename: str | None = None,
    extra_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    state = store.get(case_id)
    rows, _ = load_hypotheses_rows(case_id)
    previous = state.meta.get("hypotheses_selection") or {}
    built = (state.meta.get("hypotheses") or {}).get("built_at")
    previous_ok = bool(previous) and previous.get("hypotheses_built_at") == built
    has_picks = bool(numbers) or all_high or all_rows
    replacing_extras = extra_xlsx is not None or extra_rows is not None

    if has_picks:
        selected_ns = resolve_hypothesis_selection(
            rows,
            numbers=numbers,
            all_high=all_high,
            all_rows=all_rows,
        )
    elif keep_numbers and previous_ok:
        selected_ns = [int(n) for n in (previous.get("selected_ns") or [])]
    elif keep_numbers:
        selected_ns = []
    else:
        selected_ns = resolve_hypothesis_selection(
            rows,
            numbers=numbers,
            all_high=all_high,
            all_rows=all_rows,
        )

    if replacing_extras:
        extras = parse_auditor_hypotheses(
            generated=rows,
            extra_rows=extra_rows,
            extra_xlsx=extra_xlsx,
        )
        _store_extra_hypotheses(
            case_id,
            extras,
            extra_xlsx=extra_xlsx,
            extra_filename=extra_filename,
        )
    elif previous_ok:
        extras = load_extra_hypothesis_rows(case_id)
    else:
        extras = []
        _clear_hypothesis_extras(case_id)

    if not selected_ns and not extras:
        raise ValueError(
            "Укажите номера гипотез, например: `утверждаю гипотезы 1, 3, 5`, "
            "или приложите Excel со своими гипотезами."
        )

    by_n = {int(row["n"]): row for row in rows if str(row.get("n") or "").strip().isdigit()}
    selected = [by_n[n] for n in selected_ns if n in by_n]
    payload = {
        "selected_ns": selected_ns,
        "extra_ns": [int(row["n"]) for row in extras if str(row.get("n") or "").strip().isdigit()],
        "extra_count": len(extras),
        "extra_filename": extra_filename or previous.get("extra_filename") or "",
        "selected_at": utc_now().isoformat(),
        "hypotheses_built_at": built,
        "count": len(selected_ns) + len(extras),
    }
    if replacing_extras:
        payload["extra_filename"] = extra_filename or ""
    state.meta["hypotheses_selection"] = payload
    store.save(state)
    combined = selected + extras
    return {
        "case_id": case_id,
        "selected_ns": selected_ns,
        "extra_ns": payload["extra_ns"],
        "count": len(combined),
        "extra_count": len(extras),
        "hypotheses": [_preview_row(row) for row in combined],
    }


def _preview_row(row: dict[str, str]) -> dict[str, str]:
    return {
        "n": str(row.get("n") or ""),
        "hypothesis": str(row.get("hypothesis") or ""),
        "priority": str(row.get("priority") or ""),
        "origin": str(row.get("origin") or ""),
    }


def selected_hypothesis_rows(state: CaseState) -> list[dict[str, str]]:
    try:
        rows, _ = load_hypotheses_rows(state.case_id)
    except ValueError:
        return []
    selection = state.meta.get("hypotheses_selection") or {}
    ns = selection.get("selected_ns") or []
    built = (state.meta.get("hypotheses") or {}).get("built_at")
    selected_built = selection.get("hypotheses_built_at")
    if selected_built and built and selected_built != built:
        return []
    by_n = {int(row["n"]): row for row in rows if str(row.get("n") or "").strip().isdigit()}
    out: list[dict[str, str]] = []
    for n in ns:
        row = by_n.get(int(n))
        if row:
            out.append(row)
    extras = load_extra_hypothesis_rows(state.case_id)
    wanted_extra = [str(n) for n in (selection.get("extra_ns") or [])]
    if wanted_extra:
        by_extra = {str(row.get("n") or ""): row for row in extras}
        extras = [by_extra[n] for n in wanted_extra if n in by_extra]
    out.extend(extras)
    return out


def _write_markdown(
    path: Path,
    *,
    inspection_name: str,
    keywords: list[str],
    case_id: str,
    rows: list[dict[str, str]],
    notes: str,
    sources: list[dict],
) -> None:
    lines = [
        "# Чеклист гипотез внутренней аудиторской проверки",
        f"**{inspection_name}**",
        f"Ключевые слова: {', '.join(keywords) or '—'}. Кейс `{case_id}`.",
        "",
        "Черновик планирования СВА банка РБ. Основной файл — Excel.",
        "",
    ]
    if notes:
        lines.extend(["## Примечания", notes, ""])
    lines.append("## Гипотезы")
    for row in rows:
        lines.append(f"### {row['n']}. {row['hypothesis']}")
        lines.append(f"- Приоритет: {row['priority']}")
        lines.append(f"- Утверждение: {row['assertion']}")
        lines.append(f"- Риск: {row['risk']}")
        lines.append(f"- Разделы плана: {row['plan_sections']}")
        lines.append(f"- НПА / критерии: {row['npa_criteria']}")
        lines.append(f"- Почему это риск: {row['why_risk']}")
        lines.append(f"- Как проверить: {row['how_to_test']}")
        lines.append(f"- Что запросить: {row['evidence_request']}")
        lines.append(f"- Рабочий документ: {row['working_paper']}")
        lines.append(f"- Опора: {row['basis']}")
        lines.append("")
    append_npa_sources_markdown(lines, sources)
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def _parse_composed_hypotheses(raw: str) -> tuple[list[dict[str, str]], str]:
    try:
        return parse_hypotheses_payload(raw)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Не удалось разобрать гипотезы модели: {exc}") from exc


def _persist_hypotheses(
    state: CaseState,
    paths: ArtifactPaths,
    parsed: tuple[list[dict[str, str]], str],
    sources: list[dict],
) -> ArtifactOutcome:
    rows, notes = parsed
    state.meta.pop("hypotheses_selection", None)
    _clear_hypothesis_extras(state.case_id)
    _write_markdown(
        paths.md,
        inspection_name=state.inspection_name,
        keywords=state.keywords,
        case_id=state.case_id,
        rows=rows,
        notes=notes,
        sources=sources,
    )
    write_hypotheses_xlsx(
        paths.primary,
        inspection_name=state.inspection_name,
        keywords=state.keywords,
        case_id=state.case_id,
        rows=rows,
        notes=notes,
    )
    _json_path(state.case_id).write_text(
        json.dumps({"notes": notes, "hypotheses": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    body = "\n".join(r["hypothesis"] for r in rows)
    return ArtifactOutcome(
        body=body,
        sources=sources,
        extra={
            "schema": HYPOTHESES_SCHEMA,
            "items": knowledge_ok_count(state),
            "keywords": list(state.keywords),
            "count": len(rows),
            "xlsx_path": str(paths.primary),
            **upstream_built_at(state, "brief", "total", "program"),
            "download": HYPOTHESES_SPEC.docx_endpoint.format(case_id=state.case_id),
        },
        digest=[f"- {r['hypothesis']}" for r in rows[:8]],
    )


async def build_hypotheses_events(
    case_id: str, force: bool = False
) -> AsyncIterator[dict]:
    async for event in run_llm_artifact_events(
        case_id,
        HYPOTHESES_SPEC,
        force=force,
        start_message="Собираю саммари, программу, total и фрагменты НПА…",
        already_message="Чеклист гипотез уже собран — отдаю Excel.",
        prepare_message="Отбираю фрагменты из приложенных документов…",
        compose_message="Формулирую 8–10 гипотез проверки. Это может занять несколько минут…",
        writing_message="Собираю Excel-чеклист гипотез…",
        load_state=ingest_library,
        is_stale=_hypotheses_stale,
        prepare=collect_brief_sources,
        compose=_compose_hypotheses,
        postprocess=_parse_composed_hypotheses,
        write=_persist_hypotheses,
        compose_fail="Модель не собрала гипотезы",
    ):
        yield event


async def _compose_hypotheses(state: CaseState, sources: list[dict]) -> str:
    catalog = []
    for src in sources:
        article = src.get("article") or "фрагмент"
        url = src.get("url") or "нет URL"
        catalog.append(f"[{src['n']}] {src.get('title')} — {article} — {url}")

    cards = existing_cards(state)
    cards_block = ""
    if cards:
        cards_block = (
            "Карточки саммари по актам (ориентир по базе знаний):\n"
            f"{cards}\n"
        )
    else:
        cards_block = "Карточки саммари ещё не собраны (команда `саммари`).\n"

    total_md = read_truncated_md(resolve_total_file, state.case_id)
    if total_md:
        total_block = (
            "Саммари total (конспект из знаний модели по теме):\n"
            f"{total_md}\n"
        )
    else:
        total_block = "Саммари total ещё не собрано (команда `саммари total`).\n"

    program_md = read_truncated_md(resolve_program_file, state.case_id)
    if program_md:
        program_block = (
            "Программа проверки (черновик процедур — привязывай plan_sections к её разделам):\n"
            f"{program_md}\n"
        )
    else:
        program_block = "Программа проверки ещё не собрана (команда `программа проверки`).\n"

    user = prompt(
        "hypotheses_user",
        inspection=state.inspection_name,
        keywords=", ".join(state.keywords) or "не указаны",
        document_catalog=document_catalog(state),
        catalog="\n".join(catalog) or "список пуст — не выдумывай номера статей как факт",
        fragments=format_npa_sources(sources),
        cards_block=cards_block,
        total_block=total_block,
        program_block=program_block,
        schema=prompt("hypotheses_schema").strip(),
    )
    return await chat_complete(
        prompt("hypotheses_system"),
        user,
        timeout=settings.brief_timeout_sec,
        num_ctx=settings.ollama_num_ctx,
        num_predict=8192,
        temperature=0.2,
    )


async def build_hypotheses(case_id: str, force: bool = False) -> dict:
    return await event_result(
        build_hypotheses_events(case_id, force=force),
        "Гипотезы не собраны",
    )
