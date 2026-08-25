from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from pathlib import Path

from app.config import settings
from app.models import CaseState
from app.services.brief_docx import write_total_docx
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
from app.services.ollama_client import chat_complete
from app.storage import store

TOTAL_SCHEMA = 1
TOTAL_SPEC = ArtifactSpec(
    meta_key="total",
    directory="totals",
    file_prefix="total",
    md_name="total.md",
    sources_name="total_sources.json",
    download_suffix="total",
    docx_endpoint="/api/v1/cases/{case_id}/knowledge/total.docx",
    md_endpoint="/api/v1/cases/{case_id}/knowledge/total.md",
    docx_glob="total_*.docx",
)

TOTAL_SYSTEM = """Ты — опытный внутренний аудитор и консультант по праву Республики Беларусь.
Тебе нужно подготовить краткий конспект по теме проверки, опираясь ТОЛЬКО на свои знания
(обучение модели), а НЕ на приложенные файлы кейса и НЕ на базу знаний.

Правила:
1. Пиши по-русски, официально, конкретно. Это черновик для чтения аудитором, не заключение.
2. Опирайся на право РБ: кодексы, законы, указы, постановления, инструкции НБРБ, акты Минфина и МНС.
3. Не подменяй право РБ нормами РФ, ЕС, МСФО, если это не прямо уместно как ориентир и ты явно пометишь отличие.
4. Указывай конкретные акты, статьи, пункты, главы — насколько помнишь. Если не уверен в номере — напиши
   «уточнить по первоисточнику» и не выдумывай точный номер как факт.
5. После нормативного тезиса ставь ссылку [n] на источник из списка в конце.
6. Не ставь аудиторское суждение («нарушение / не нарушение»). Не выдумывай даты, ФИО, номера приказов банка.
7. В конце ОБЯЗАТЕЛЬНО дай раздел ## Источники в формате по одной строке на ссылку:
   [n] Название акта — статья/пункт/глава — URL (или «URL неизвестен»)
8. URL указывай только официальные домены РБ (pravo.by, pravo.gov.by, nbrb.by, minfin.gov.by, nalog.gov.by
   и т.п.), если помнишь. Иначе пиши «URL неизвестен».
"""

TOTAL_SECTIONS = """
Верни markdown со следующими разделами (заголовки ## сохраняй):

## Суть темы
2–4 предложения: о чём проверка и почему это важно для банка / организации в РБ.

## Ключевые нормативные акты
Список актов с кратким «зачем нужен» и ссылками [n].

## Самое важное по существу
Маркированный список главных правовых положений, рисков и контрольных точек по теме.
Каждый пункт — с отсылкой к статье/пункту и [n].

## На что обратить внимание аудитору
Практические акценты: типичные отклонения, что запросить, где чаще путают нормы.

## Ограничения конспекта
Явно напиши, что текст из знаний модели, не из скачанных актов кейса; номера и редакции
нужно сверять с первоисточником / саммари по базе знаний.

## Источники
[1] … — … — …
[2] …
"""

_SOURCE_LINE_RE = re.compile(
    r"^\s*\[(\d+)\]\s*(.+?)(?:\s*[—–\-]\s*(.+?))?(?:\s*[—–\-]\s*(https?://\S+|URL неизвестен))?\s*$",
    re.I,
)


def _total_dir(case_id: str) -> Path:
    return artifact_dir(case_id, TOTAL_SPEC)


def _docx_path(case_id: str, inspection_name: str) -> Path:
    return artifact_docx_path(case_id, inspection_name, TOTAL_SPEC)


def _md_path(case_id: str) -> Path:
    return artifact_md_path(case_id, TOTAL_SPEC)


def _sources_path(case_id: str) -> Path:
    return artifact_sources_path(case_id, TOTAL_SPEC)


def total_download_name(inspection_name: str, case_id: str = "", ext: str = "docx") -> str:
    _ = case_id
    return artifact_download_name(inspection_name, TOTAL_SPEC, ext=ext)


def resolve_total_file(case_id: str, kind: str) -> Path | None:
    return resolve_artifact_file(case_id, TOTAL_SPEC, kind)


def total_status(case_id: str) -> dict:
    return artifact_status(case_id, TOTAL_SPEC)


def _total_stale(state: CaseState) -> bool:
    meta = state.meta.get("total") or {}
    path = Path(meta["docx_path"]) if meta.get("docx_path") else None
    if not path or not path.exists():
        return True
    if meta.get("schema") != TOTAL_SCHEMA:
        return True
    if meta.get("keywords") != list(state.keywords):
        return True
    if meta.get("inspection_name") != state.inspection_name:
        return True
    if meta.get("period") != (state.period or None):
        return True
    return False


def parse_total_sources(md: str) -> tuple[str, list[dict]]:
    """Split body and ## Источники; parse [n] lines into source dicts."""
    text = (md or "").strip()
    marker = re.search(r"(?im)^##\s*Источники\s*$", text)
    if not marker:
        return text, []
    body = text[: marker.start()].rstrip()
    tail = text[marker.end() :].lstrip("\n")
    sources: list[dict] = []
    for raw in tail.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _SOURCE_LINE_RE.match(line)
        if not match:
            continue
        n = int(match.group(1))
        title = (match.group(2) or "").strip(" .")
        article = (match.group(3) or "").strip(" .")
        url_raw = (match.group(4) or "").strip()
        url = "" if not url_raw or url_raw.lower() == "url неизвестен" else url_raw
        # If only two parts and middle looks like URL, treat as title — url
        if not url and article.lower().startswith("http"):
            url, article = article, ""
        sources.append(
            {
                "n": n,
                "title": title or "акт",
                "article": article,
                "url": url,
                "excerpt": "",
            }
        )
    sources.sort(key=lambda s: int(s["n"]))
    return body, sources


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
            s = line.strip()
            if s.startswith("- ") or s.startswith("* "):
                item = s[2:].strip()
                if len(item) > 140:
                    item = item[:137] + "…"
                out.append(f"- {item}")
                if len(out) >= limit:
                    break
    if not out:
        for line in (body or "").splitlines():
            if line.startswith("## "):
                out.append(f"- {line[3:].strip()}")
                if len(out) >= limit:
                    break
    return out


def _save_total_meta(
    state: CaseState,
    *,
    docx: Path,
    md: Path,
    sources: list[dict],
    body: str,
) -> dict:
    return save_artifact_meta(
        state,
        TOTAL_SPEC,
        docx=docx,
        md=md,
        sources=sources,
        body=body,
        extra={
            "keywords": list(state.keywords),
            "period": state.period or None,
            "schema": TOTAL_SCHEMA,
            "source": "model_knowledge",
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
        "# Конспект по теме (знания модели)",
        f"**{inspection_name}**",
        f"Период: {period or 'не указан'}. Ключевые слова: {', '.join(keywords) or '—'}. Кейс `{case_id}`.",
        "",
        "Черновик из знаний LLM, без опоры на скачанные акты базы знаний. "
        "Номера `[n]` — ссылки на список источников в конце. Сверяйте с первоисточником.",
        "",
        body.strip(),
        "",
    ]
    if sources:
        lines.append("## Источники")
        for src in sources:
            article = src.get("article") or ""
            url = src.get("url") or "URL неизвестен"
            mid = f" — {article}" if article else ""
            lines.append(f"[{src['n']}] {src.get('title')}{mid} — {url}")
            lines.append("")
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


async def _compose_total(state: CaseState) -> str:
    target = 4 * settings.brief_chars_per_page
    user = f"""Составь краткий конспект и самое важное по теме проверки — из своих знаний права РБ.
Не используй тексты файлов кейса (их нет в этом запросе).

Название проверки: {state.inspection_name}
Ключевые слова: {", ".join(state.keywords) or "не указаны"}
Период: {state.period or "не указан"}

{TOTAL_SECTIONS}

Объём: примерно {target}–{target + 1500} знаков основного текста (без списка источников).
Сфокусируйся на самом важном для аудитора, а не на полном пересказе кодексов.
"""
    return await chat_complete(
        TOTAL_SYSTEM,
        user,
        timeout=settings.brief_timeout_sec,
        num_ctx=settings.ollama_num_ctx,
        num_predict=6144,
    )


async def build_total_events(case_id: str, force: bool = False) -> AsyncIterator[dict]:
    timer = ElapsedTimer()
    elapsed = timer.ms

    yield {
        "type": "status",
        "message": "Готовлю конспект по знаниям модели…",
        "elapsed_ms": elapsed(),
    }
    state = store.get(case_id)

    if not force and not _total_stale(state):
        meta = total_status(case_id)
        yield {
            "type": "status",
            "message": "Саммари total уже собран — отдаю файл.",
            "elapsed_ms": elapsed(),
        }
        yield {"type": "result", **meta, "digest": [], "elapsed_ms": elapsed()}
        return

    yield {
        "type": "status",
        "message": "Модель пишет конспект по теме из своих знаний. Это может занять несколько минут…",
        "elapsed_ms": elapsed(),
    }
    try:
        raw = await _compose_total(state)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Модель не собрала саммари total: {exc}") from exc
    if not (raw or "").strip():
        raise ValueError("Модель вернула пустой саммари total.")

    body, sources = parse_total_sources(raw)
    if not body.strip():
        body = raw.strip()

    md = _md_path(case_id)
    docx = _docx_path(case_id, state.inspection_name)
    yield {
        "type": "status",
        "message": "Собираю Word с саммари total…",
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
    write_total_docx(
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
    meta = _save_total_meta(state, docx=docx, md=md, sources=sources, body=body)
    meta["digest"] = _digest(body)
    yield {"type": "result", **meta, "elapsed_ms": elapsed()}


async def build_total(case_id: str, force: bool = False) -> dict:
    return await event_result(
        build_total_events(case_id, force=force),
        "Саммари total не собран",
    )
