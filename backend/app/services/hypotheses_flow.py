from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from app.config import settings
from app.models import CaseState
from app.prompts import prompt
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
from app.services.hypotheses_xlsx import write_hypotheses_xlsx
from app.services.knowledge_ingest import ingest_library
from app.services.ollama_client import chat_complete, extract_json_value
from app.services.program_flow import (
    _document_catalog,
    _existing_cards,
    resolve_program_file,
)
from app.services.total_flow import resolve_total_file
from app.storage import store

HYPOTHESES_SCHEMA = 1
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


def _xlsx_path(case_id: str, inspection_name: str) -> Path:
    return artifact_docx_path(case_id, inspection_name, HYPOTHESES_SPEC)


def _md_path(case_id: str) -> Path:
    return artifact_md_path(case_id, HYPOTHESES_SPEC)


def _sources_path(case_id: str) -> Path:
    return artifact_sources_path(case_id, HYPOTHESES_SPEC)


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
    meta = artifact_status(case_id, HYPOTHESES_SPEC)
    meta["download"] = HYPOTHESES_SPEC.docx_endpoint.format(case_id=case_id)
    return meta


def _hypotheses_stale(state: CaseState) -> bool:
    meta = state.meta.get("hypotheses") or {}
    path = Path(meta["docx_path"]) if meta.get("docx_path") else None
    if not path or not path.exists():
        return True
    if meta.get("schema") != HYPOTHESES_SCHEMA:
        return True
    ok_items = sum(1 for i in state.knowledge if i.extract_status == "ok")
    if meta.get("items") != ok_items:
        return True
    if meta.get("keywords") != list(state.keywords):
        return True
    if meta.get("inspection_name") != state.inspection_name:
        return True
    if meta.get("brief_built_at") != (state.meta.get("brief") or {}).get("built_at"):
        return True
    if meta.get("total_built_at") != (state.meta.get("total") or {}).get("built_at"):
        return True
    if meta.get("program_built_at") != (state.meta.get("program") or {}).get("built_at"):
        return True
    return False


def _read_md(resolver, case_id: str, limit: int = 14000) -> str:
    path = resolver(case_id, "md")
    if not path or not path.exists():
        return ""
    text = path.read_text(encoding="utf-8").strip()
    if len(text) > limit:
        return text[:limit] + "\n…"
    return text


def _format_sources(sources: list[dict]) -> str:
    blocks = []
    for fr in sources[:40]:
        article = fr.get("article") or "фрагмент без номера статьи"
        url = fr.get("url") or "URL в библиотеке не зафиксирован"
        text = fr.get("text") or fr.get("excerpt") or ""
        blocks.append(f"[{fr['n']}] {article}\nисточник: {url}\n{text}")
    return "\n\n---\n\n".join(blocks) if blocks else "Фрагментов НПА нет."


def _normalize_priority(value: str) -> str:
    key = (value or "").strip().lower()
    return _PRIORITY_MAP.get(key, "средний")


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
    return rows, notes


def _write_markdown(
    path: Path,
    *,
    inspection_name: str,
    period: str | None,
    keywords: list[str],
    case_id: str,
    rows: list[dict[str, str]],
    notes: str,
    sources: list[dict],
) -> None:
    lines = [
        "# Чеклист гипотез внутренней аудиторской проверки",
        f"**{inspection_name}**",
        f"Период: {period or 'не указан'}. Ключевые слова: {', '.join(keywords) or '—'}. Кейс `{case_id}`.",
        "",
        "Черновик планирования СВА банка РБ. Основной файл — Excel.",
        "",
    ]
    if notes:
        lines.extend(["## Примечания", notes, ""])
    lines.append("## Гипотезы")
    for row in rows:
        lines.append(f"### {row['n']}. {row['hypothesis']}")
        lines.append(f"- Утверждение: {row['assertion']}")
        lines.append(f"- Риск: {row['risk']}")
        lines.append(f"- Разделы плана: {row['plan_sections']}")
        lines.append(f"- НПА / критерии: {row['npa_criteria']}")
        lines.append(f"- Почему это риск: {row['why_risk']}")
        lines.append(f"- Как проверить: {row['how_to_test']}")
        lines.append(f"- Что запросить: {row['evidence_request']}")
        lines.append(f"- Рабочий документ: {row['working_paper']}")
        lines.append(f"- Приоритет: {row['priority']}")
        lines.append(f"- Опора: {row['basis']}")
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


def _save_meta(
    state: CaseState,
    *,
    xlsx: Path,
    md: Path,
    sources: list[dict],
    rows: list[dict[str, str]],
) -> dict:
    body = "\n".join(r["hypothesis"] for r in rows)
    return save_artifact_meta(
        state,
        HYPOTHESES_SPEC,
        docx=xlsx,
        md=md,
        sources=sources,
        body=body,
        extra={
            "schema": HYPOTHESES_SCHEMA,
            "items": sum(1 for i in state.knowledge if i.extract_status == "ok"),
            "keywords": list(state.keywords),
            "count": len(rows),
            "xlsx_path": str(xlsx),
            "brief_built_at": (state.meta.get("brief") or {}).get("built_at"),
            "total_built_at": (state.meta.get("total") or {}).get("built_at"),
            "program_built_at": (state.meta.get("program") or {}).get("built_at"),
            "download": HYPOTHESES_SPEC.docx_endpoint.format(case_id=state.case_id),
        },
    )


async def _compose_hypotheses(state: CaseState, sources: list[dict]) -> str:
    catalog = []
    for src in sources:
        article = src.get("article") or "фрагмент"
        url = src.get("url") or "нет URL"
        catalog.append(f"[{src['n']}] {src.get('title')} — {article} — {url}")

    cards = _existing_cards(state)
    cards_block = ""
    if cards:
        cards_block = (
            "Карточки саммари по актам (ориентир по базе знаний):\n"
            f"{cards}\n"
        )
    else:
        cards_block = "Карточки саммари ещё не собраны (команда `саммари`).\n"

    total_md = _read_md(resolve_total_file, state.case_id)
    if total_md:
        total_block = (
            "Саммари total (конспект из знаний модели по теме):\n"
            f"{total_md}\n"
        )
    else:
        total_block = "Саммари total ещё не собрано (команда `саммари total`).\n"

    program_md = _read_md(resolve_program_file, state.case_id)
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
        period=state.period or "не указан",
        document_catalog=_document_catalog(state),
        catalog="\n".join(catalog) or "список пуст — не выдумывай номера статей как факт",
        fragments=_format_sources(sources),
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


async def build_hypotheses_events(
    case_id: str, force: bool = False
) -> AsyncIterator[dict]:
    timer = ElapsedTimer()
    elapsed = timer.ms

    yield {
        "type": "status",
        "message": "Собираю саммари, программу, total и фрагменты НПА…",
        "elapsed_ms": elapsed(),
    }
    state = ingest_library(case_id)

    if not force and not _hypotheses_stale(state):
        meta = hypotheses_status(case_id)
        yield {
            "type": "status",
            "message": "Чеклист гипотез уже собран — отдаю Excel.",
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
        "message": "Формулирую 8–10 гипотез проверки. Это может занять несколько минут…",
        "elapsed_ms": elapsed(),
    }
    try:
        raw = await _compose_hypotheses(state, sources)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Модель не собрала гипотезы: {exc}") from exc

    try:
        rows, notes = parse_hypotheses_payload(raw)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Не удалось разобрать гипотезы модели: {exc}") from exc

    md = _md_path(case_id)
    xlsx = _xlsx_path(case_id, state.inspection_name)
    yield {
        "type": "status",
        "message": "Собираю Excel-чеклист гипотез…",
        "elapsed_ms": elapsed(),
    }
    _write_markdown(
        md,
        inspection_name=state.inspection_name,
        period=state.period,
        keywords=state.keywords,
        case_id=case_id,
        rows=rows,
        notes=notes,
        sources=sources,
    )
    write_hypotheses_xlsx(
        xlsx,
        inspection_name=state.inspection_name,
        period=state.period,
        keywords=state.keywords,
        case_id=case_id,
        rows=rows,
        notes=notes,
    )
    payload = {"notes": notes, "hypotheses": rows}
    _json_path(case_id).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _sources_path(case_id).write_text(
        json.dumps(sources, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    meta = _save_meta(state, xlsx=xlsx, md=md, sources=sources, rows=rows)
    meta["digest"] = [f"- {r['hypothesis']}" for r in rows[:8]]
    yield {"type": "result", **meta, "elapsed_ms": elapsed()}


async def build_hypotheses(case_id: str, force: bool = False) -> dict:
    return await event_result(
        build_hypotheses_events(case_id, force=force),
        "Гипотезы не собраны",
    )
