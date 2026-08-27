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
from app.services.hypotheses_xlsx import write_hypotheses_xlsx
from app.services.knowledge_ingest import ingest_library
from app.services.ollama_client import chat_complete, extract_json_value
from app.services.program_flow import resolve_program_file
from app.services.total_flow import resolve_total_file
from app.storage import store

HYPOTHESES_SCHEMA = 2
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
) -> dict[str, Any]:
    state = store.get(case_id)
    rows, _ = load_hypotheses_rows(case_id)
    selected_ns = resolve_hypothesis_selection(
        rows,
        numbers=numbers,
        all_high=all_high,
        all_rows=all_rows,
    )
    by_n = {int(row["n"]): row for row in rows if str(row.get("n") or "").strip().isdigit()}
    selected = [by_n[n] for n in selected_ns]
    payload = {
        "selected_ns": selected_ns,
        "selected_at": utc_now().isoformat(),
        "hypotheses_built_at": (state.meta.get("hypotheses") or {}).get("built_at"),
        "count": len(selected_ns),
    }
    state.meta["hypotheses_selection"] = payload
    store.save(state)
    return {
        "case_id": case_id,
        "selected_ns": selected_ns,
        "count": len(selected_ns),
        "hypotheses": [
            {
                "n": row.get("n"),
                "hypothesis": row.get("hypothesis"),
                "priority": row.get("priority"),
            }
            for row in selected
        ],
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
