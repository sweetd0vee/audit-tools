from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

from app.config import settings
from app.filenames import safe_stem
from app.models import CaseState
from app.services.brief_docx import write_program_docx
from app.services.brief_flow import collect_brief_sources
from app.services.citations import pages_estimate
from app.services.knowledge_flow import ingest_library
from app.services.ollama_client import chat_complete
from app.storage import store

PROGRAM_SYSTEM = """Ты — внутренний аудитор банка в Республике Беларусь, сотрудник службы внутреннего аудита.
Тебе поручено подготовить черновик ПРОГРАММЫ АУДИТОРСКОЙ ПРОВЕРКИ по конкретной теме.

Роль и рамки:
1. Ты готовишь рабочий документ планирования проверки для банка РБ, а не заключение и не акт.
2. Опирайся на право Республики Беларусь: Банковский кодекс, Гражданский кодекс, Налоговый кодекс, законы РБ, нормативные правовые акты и инструкции Национального банка Республики Беларусь, акты Минфина и МНС, внутренние подходы службы внутреннего аудита банка.
3. Не подменяй право РБ нормами Российской Федерации, ЕС, МСФО/IFRS, если их нет во фрагментах.
4. Не ставь аудиторское суждение («нарушение / не нарушение», «эффективно / неэффективно»). Не подписывай программу от имени руководителя СВА.
5. Номера статей, пунктов и инструкций указывай только если они есть во фрагментах приложенных документов. После нормативного тезиса ставь ссылку [n]. Не ссылайся на номер, которого нет в списке.
6. Если фрагментов мало или акта нет — напиши процедуру на уровне направления проверки и явно пометь: «критерий уточнить по первоисточнику / акт не приложен».
7. Пиши по-русски, официально, конкретно, без канцелярской воды и без выдуманных дат, ФИО, номеров приказов банка.
8. Программа должна быть практической: что смотреть, у кого запросить, какой критерий, какой рабочий документ получится.

Типовая логика внутренней аудиторской проверки банка РБ (отрази в структуре):
- цель и задачи проверки по теме;
- объект, границы, период;
- существенные риски процесса (операционный, правовой, комплаенс, бухгалтерский, налоговый — только если уместны теме);
- нормативные критерии из приложенных НПА;
- аудиторские процедуры по направлениям;
- источники доказательств (договоры, карточки счетов, выписки, решения органов банка, налоговые регистры — без выдумывания конкретных номеров дел клиента);
- подход к выборке;
- рабочие документы;
- ограничения черновика.
"""

PROGRAM_SECTIONS = """
Верни программу в markdown со следующими разделами (заголовки ## сохраняй):

## 1. Общие сведения
Объект проверки, тема, период, основание (плановая/внеплановая — если неизвестно, напиши «уточняется руководителем СВА»), заказчик внутри банка.

## 2. Цель проверки
Одна-две формулировки: дать независимую оценку соблюдения законодательства РБ, нормативных актов НБРБ и внутренних документов банка по теме проверки.

## 3. Задачи проверки
Нумерованный список 5–10 задач, привязанных к ключевым словам и рискам темы.

## 4. Объект, границы и период
Что входит в объём; что сознательно не входит (out of scope), чтобы программа не расползалась.

## 5. Нормативные критерии
Список актов из библиотеки кейса. Для каждого: зачем нужен в этой проверке; ключевые статьи/пункты с [n], если они есть во фрагментах.

## 6. Существенные риски и направления
Риски процесса и контрольные вопросы аудитора. Не путай риск банка с «риском модели».

## 7. Аудиторские процедуры
Это ядро документа. Для каждой процедуры выдай блок:

### Процедура N. Краткое название
- Направление / вопрос проверки:
- Что запросить у банка (источник доказательств):
- Как проверить (метод: просмотр, сверка, пересчёт, сопоставление с нормой, запрос):
- Критерий (норма РБ): … [n]
- На что обратить внимание / типичные отклонения:
- Рабочий документ:

Сделай 8–15 процедур, покрывающих тему проверки, а не пересказ кодексов.

## 8. Выборка
Принцип отбора (существенность, риск, сплошная по ключевым договорам / период). Без выдуманных объёмов выборки в штуках, если данных клиента нет.

## 9. Рабочие документы и результаты
Какие WP и реестры находок готовятся. Статус всех черновиков — draft.

## 10. Ограничения
Черновик для чтения глазами. Цитату сверять с файлом в библиотеке. Клиентские факты в этот контур ещё не входят.
"""


def _program_dir(case_id: str) -> Path:
    path = store.case_dir(case_id) / "programs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _docx_path(case_id: str, inspection_name: str) -> Path:
    stem = safe_stem(inspection_name or "proverka")
    return _program_dir(case_id) / f"programma_{stem}_{case_id}.docx"


def _md_path(case_id: str) -> Path:
    return _program_dir(case_id) / "program.md"


def _sources_path(case_id: str) -> Path:
    return _program_dir(case_id) / "program_sources.json"


def program_download_name(inspection_name: str, case_id: str = "", ext: str = "docx") -> str:
    _ = case_id
    stem = safe_stem(inspection_name or "proverka")
    suffix = (ext or "docx").lstrip(".")
    if suffix == "md":
        return f"{stem}_programma.md"
    return f"{stem}_programma.{suffix}"


def resolve_program_file(case_id: str, kind: str) -> Path | None:
    state = store.get(case_id)
    meta = state.meta.get("program") or {}
    key = "docx_path" if kind == "docx" else "md_path"
    stored = meta.get(key)
    if stored and Path(stored).exists():
        return Path(stored)
    if kind == "docx":
        candidate = _docx_path(case_id, state.inspection_name)
        if candidate.exists():
            return candidate
        found = sorted(_program_dir(case_id).glob("programma_*.docx"))
        return found[-1] if found else None
    candidate = _md_path(case_id)
    return candidate if candidate.exists() else None


def program_status(case_id: str) -> dict:
    state = store.get(case_id)
    meta = dict(state.meta.get("program") or {})
    docx = Path(meta["docx_path"]) if meta.get("docx_path") else _docx_path(case_id, state.inspection_name)
    ready = docx.exists()
    meta.update(
        {
            "case_id": case_id,
            "ready": ready,
            "docx_path": str(docx) if ready else meta.get("docx_path"),
            "download": f"/api/v1/cases/{case_id}/knowledge/program.docx",
            "markdown": f"/api/v1/cases/{case_id}/knowledge/program.md",
            "inspection_name": state.inspection_name,
        }
    )
    return meta


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
            if len(body) > 2500:
                body = body[:2500] + "\n…"
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
    meta = {
        "built_at": datetime.utcnow().isoformat(),
        "docx_path": str(docx),
        "md_path": str(md),
        "items": sum(1 for i in state.knowledge if i.extract_status == "ok"),
        "keywords": list(state.keywords),
        "citations": len(sources),
        "chars": len(body),
        "pages_estimate": pages_estimate(body, settings.brief_chars_per_page),
        "download": f"/api/v1/cases/{state.case_id}/knowledge/program.docx",
        "markdown": f"/api/v1/cases/{state.case_id}/knowledge/program.md",
        "ready": True,
        "case_id": state.case_id,
        "inspection_name": state.inspection_name,
    }
    state.meta["program"] = meta
    store.save(state)
    return meta


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
    user = f"""Составь программу аудиторской проверки службы внутреннего аудита банка РБ.

Название проверки: {state.inspection_name}
Ключевые слова: {", ".join(state.keywords) or "не указаны"}
Период: {state.period or "не указан"}

Документы кейса (утверждённые / скачанные / приложенные):
{_document_catalog(state)}

Список фрагментов для ссылок [n]:
{chr(10).join(catalog) or "список пуст — не выдумывай номера статей как факт"}

Фрагменты приложенных документов:
{_format_sources(sources)}
"""
    if cards:
        user += f"""

Карточки актов (если уже собрано саммари — используй как ориентир, но пиши программу процедур, а не пересказ карточек):
{cards}
"""
    user += f"""
{PROGRAM_SECTIONS}

Объём: примерно {target}–{target + 2500} знаков. Ядро — раздел «Аудиторские процедуры».
"""
    return await chat_complete(
        PROGRAM_SYSTEM,
        user,
        timeout=settings.brief_timeout_sec,
        num_ctx=settings.ollama_num_ctx,
        num_predict=8192,
    )


async def build_program_events(case_id: str, force: bool = False) -> AsyncIterator[dict]:
    t0 = datetime.utcnow()

    def elapsed() -> int:
        return int((datetime.utcnow() - t0).total_seconds() * 1000)

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
    result: dict | None = None
    async for event in build_program_events(case_id, force=force):
        if event.get("type") == "result":
            result = event
    if not result:
        raise ValueError("Программа проверки не собрана")
    return result
