"""
title: Аудитор
author: audit-tools
version: 0.1.0
license: MIT
description: Агент проверки банка РБ. Кейс НПА → HITL → download → цитаты. Цикл в коде, не ReAct 35B.
requirements: httpx
"""

from __future__ import annotations

import re
from typing import Any, Awaitable, Callable, Optional

import httpx
from pydantic import BaseModel, Field

Emitter = Optional[Callable[[Any], Awaitable[None]]]

CASE_MARK = re.compile(r"<!--audit-case:([a-z0-9]+)-->")
YEAR_RE = re.compile(r"\b(20\d{2}(?:\s*[-–]\s*20\d{2})?)\b")
APPROVE_RE = re.compile(
    r"(утвержд\w*|подтвержд\w*|выбираю|скачивай|скачай|бери\s+(эти\s+)?акты)",
    re.I,
)
REJECT_APPROVE_RE = re.compile(r"\bне\s+утвержд", re.I)
HEX_ID_RE = re.compile(r"\b([a-f0-9]{8,12})\b", re.I)
HELP = """Я агент внутренней проверки: собираю библиотеку НПА, жду вашего утверждения, качаю акты, отвечаю цитатами.

Напишите проверку, например:
Проверка аренды коммерческой недвижимости, 2025, аренда, валюта, НДС

Дальше: «утверждаю 1, 2, 4» или «утверждаю все обязательные».
Ссылку pravo.by можно сразу: «к пункту 3 url https://pravo.by/...»
Когда библиотека готова — спрашивайте норму. Нет фрагмента — скажу, что в библиотеке этого нет.
"""


class Pipe:
    class Valves(BaseModel):
        AUDIT_API: str = Field(
            default="http://backend:8100",
            description="Audit Tool Server. Compose: http://backend:8100. С хоста: http://localhost:8100",
        )
        TIMEOUT_SEC: int = Field(default=300, description="Таймаут propose/download")

    def __init__(self) -> None:
        self.type = "pipe"
        self.id = "auditor"
        self.name = "Аудитор"
        self.valves = self.Valves()

    def pipes(self) -> list[dict[str, str]]:
        return [{"id": "auditor", "name": "Аудитор"}]

    async def pipe(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __event_emitter__: Emitter = None,
        **kwargs,
    ) -> str:
        text = _last_user_text(body)
        case_id = _case_id_from_messages(body.get("messages") or [])
        api = self.valves.AUDIT_API.rstrip("/")
        timeout = float(self.valves.TIMEOUT_SEC)

        if not text.strip() or text.strip().lower() in {"помощь", "help", "/help", "?"}:
            return HELP

        await _status(__event_emitter__, "Смотрю фазу проверки…")

        try:
            if _is_approve(text):
                if not case_id:
                    return "Нет кейса в этом чате. Сначала опишите проверку."
                return await self._approve(api, timeout, case_id, text, __event_emitter__)

            if _is_status(text):
                if case_id:
                    return await self._status_case(api, timeout, case_id)
                return await self._list_cases(api, timeout)

            parsed = _parse_new_case(text)
            if parsed and not _looks_like_question(text):
                return await self._start(api, timeout, parsed, __event_emitter__)

            if case_id and _looks_like_question(text):
                return await self._ask(api, timeout, case_id, text, __event_emitter__)

            if parsed:
                return await self._start(api, timeout, parsed, __event_emitter__)

            if case_id:
                return await self._ask(api, timeout, case_id, text, __event_emitter__)

            return HELP
        except httpx.HTTPError as exc:
            return f"Audit Tool Server недоступен (`{api}`): {exc}"
        except Exception as exc:  # noqa: BLE001
            return f"Агент остановился: {exc}"
        finally:
            await _status(__event_emitter__, "", done=True)

    async def _start(
        self,
        api: str,
        timeout: float,
        parsed: dict[str, Any],
        emitter: Emitter,
    ) -> str:
        await _status(emitter, "Создаю кейс…")
        created = await _req(
            "POST",
            f"{api}/api/v1/cases",
            timeout,
            json={
                "inspection_name": parsed["inspection_name"],
                "keywords": parsed["keywords"],
                "period": parsed.get("period") or None,
            },
        )
        case_id = created["case_id"]
        await _status(emitter, "Модель предлагает список НПА (это может занять минуты)…")
        proposed = await _req("POST", f"{api}/api/v1/cases/{case_id}/propose", timeout)
        docs = proposed.get("documents") or []
        lines = [
            f"Кейс `{case_id}` — {created.get('inspection_name')}",
            f"Период: {parsed.get('period') or 'не указан'}. Keywords: {', '.join(parsed['keywords']) or '—'}",
            "",
            "Предлагаю акты. **Ничего не скачаю**, пока не утвердите номера или id.",
            "",
            _format_docs(docs),
            "",
            "Напишите: `утверждаю 1, 2, 4` или `утверждаю все обязательные`.",
            "Если знаете ссылку: `к 3 url https://pravo.by/...`",
            f"<!--audit-case:{case_id}-->",
        ]
        return "\n".join(lines)

    async def _approve(
        self,
        api: str,
        timeout: float,
        case_id: str,
        text: str,
        emitter: Emitter,
    ) -> str:
        state = await _req("GET", f"{api}/api/v1/cases/{case_id}", timeout)
        docs = state.get("documents") or []
        ids, manuals = _resolve_approval(text, docs)
        if not ids:
            return (
                "Не поняла, какие акты утвердить. Напишите номера из списка "
                "(`утверждаю 1, 2`) или `утверждаю все обязательные`.\n"
                f"<!--audit-case:{case_id}-->"
            )
        await _status(emitter, "Фиксирую выбор аудитора…")
        body: dict[str, Any] = {"document_ids": ids}
        if manuals:
            body["manual_urls"] = manuals
        await _req("POST", f"{api}/api/v1/cases/{case_id}/select", timeout, json=body)
        await _status(emitter, "Качаю утверждённые акты (allowlist РБ)…")
        downloaded = await _req("POST", f"{api}/api/v1/cases/{case_id}/download", timeout)
        await _status(emitter, "Синхронизирую Knowledge…")
        sync_note = ""
        try:
            sync = await _req(
                "POST",
                f"{api}/api/v1/cases/{case_id}/knowledge/openwebui/sync",
                timeout,
                json={},
            )
            name = sync.get("knowledge_name") or sync.get("name") or "коллекция кейса"
            sync_note = f"Open WebUI Knowledge: {name}."
        except Exception as exc:  # noqa: BLE001
            sync_note = (
                f"Knowledge не синхронизировался ({exc}). "
                "Можно спросить норму всё равно — ответ пойдёт через индекс сервера."
            )
        ok = downloaded.get("downloaded", 0)
        failed = downloaded.get("failed", 0)
        fail_lines = []
        for d in downloaded.get("documents") or []:
            if d.get("selected") and d.get("download_status") not in {"ok", "skipped", None}:
                fail_lines.append(
                    f"- {d.get('title')}: {d.get('download_status')} — {d.get('download_error') or 'нет URL'}"
                )
        extra = ""
        if fail_lines:
            extra = (
                "\nНе скачалось:\n"
                + "\n".join(fail_lines)
                + "\nПришлите официальный URL: `к <номер> url https://...`\n"
            )
        return (
            f"Кейс `{case_id}`: скачано {ok}, ошибок {failed}. {sync_note}\n"
            f"{extra}\n"
            "Можно спрашивать норму. Нет фрагмента в библиотеке — скажу, что этого нет.\n"
            f"<!--audit-case:{case_id}-->"
        )

    async def _ask(
        self,
        api: str,
        timeout: float,
        case_id: str,
        question: str,
        emitter: Emitter,
    ) -> str:
        await _status(emitter, "Ищу цитату в библиотеке кейса…")
        try:
            result = await _req(
                "POST",
                f"{api}/api/v1/cases/{case_id}/knowledge/ask",
                timeout,
                json={"question": question},
            )
        except Exception as exc:  # noqa: BLE001
            return (
                f"Не могу ответить по базе кейса `{case_id}`: {exc}\n"
                "Сначала утвердите список и дождитесь скачивания.\n"
                f"<!--audit-case:{case_id}-->"
            )
        sources = result.get("sources") or []
        cites = []
        for s in sources[:6]:
            title = s.get("title") or s.get("filename") or "фрагмент"
            excerpt = (s.get("excerpt") or "").replace("\n", " ").strip()
            if len(excerpt) > 220:
                excerpt = excerpt[:220] + "…"
            cites.append(f"- [{s.get('n')}] {title}: {excerpt}")
        cite_block = "\n".join(cites) if cites else "_Цитат нет — не считайте ответ нормой._"
        return (
            f"{result.get('answer', '').strip()}\n\n"
            f"**Откуда:**\n{cite_block}\n"
            f"<!--audit-case:{case_id}-->"
        )

    async def _status_case(self, api: str, timeout: float, case_id: str) -> str:
        state = await _req("GET", f"{api}/api/v1/cases/{case_id}", timeout)
        return _format_case(state) + f"\n<!--audit-case:{case_id}-->"

    async def _list_cases(self, api: str, timeout: float) -> str:
        rows = await _req("GET", f"{api}/api/v1/cases", timeout)
        if not rows:
            return "Кейсов нет. Опишите проверку, чтобы создать первый."
        lines = ["Проверки:"]
        for row in rows[:20]:
            lines.append(
                f"- `{row.get('case_id')}` {row.get('status')} — {row.get('inspection_name')}"
            )
        lines.append("\nНапишите название проверки, чтобы начать новую, или вопрос в чате с уже созданным кейсом.")
        return "\n".join(lines)


def _last_user_text(body: dict) -> str:
    for message in reversed(body.get("messages") or []):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") in (None, "text"):
                    parts.append(str(item.get("text") or ""))
            return "\n".join(parts).strip()
        return str(content or "").strip()
    return ""


def _case_id_from_messages(messages: list) -> Optional[str]:
    for message in reversed(messages):
        content = message.get("content")
        if not isinstance(content, str):
            continue
        found = CASE_MARK.findall(content)
        if found:
            return found[-1]
    return None


def _is_approve(text: str) -> bool:
    if REJECT_APPROVE_RE.search(text):
        return False
    return bool(APPROVE_RE.search(text))


def _is_status(text: str) -> bool:
    t = text.strip().lower()
    return t in {"статус", "status", "кейсы", "проверки", "/status"} or t.startswith("статус ")


def _looks_like_question(text: str) -> bool:
    t = text.strip().lower()
    if t.endswith("?"):
        return True
    return bool(
        re.match(
            r"^(какой|какая|какие|каков|что |где |когда |срок|можно ли|нужно ли|какой срок)",
            t,
        )
    )


def _parse_new_case(text: str) -> Optional[dict[str, Any]]:
    raw = text.strip()
    if len(raw) < 8:
        return None
    if _is_approve(raw) or _is_status(raw):
        return None
    period_match = YEAR_RE.search(raw)
    period = period_match.group(1).replace(" ", "") if period_match else None
    parts = [p.strip(" .;") for p in re.split(r"[,;\n]", raw) if p.strip()]
    if not parts:
        return None
    name = parts[0]
    if period and name == period_match.group(0):
        return None
    keywords = []
    for part in parts[1:]:
        if YEAR_RE.fullmatch(part.replace(" ", "")):
            continue
        keywords.append(part)
    looks_like_case = bool(
        re.search(r"проверк|аудит|аренда|кредит|валют|касс|нпа", raw, re.I)
    ) or (len(name) >= 12 and not _looks_like_question(raw))
    if not looks_like_case:
        return None
    return {
        "inspection_name": name,
        "keywords": keywords,
        "period": period,
    }


def _resolve_approval(text: str, docs: list[dict]) -> tuple[list[str], dict[str, str]]:
    manuals: dict[str, str] = {}
    for match in re.finditer(
        r"(?:к|пункт|акт|номер|id)\s*([a-f0-9]{8,12}|\d{1,2})\s+(?:url|ссылка)\s+(https?://\S+)",
        text,
        re.I,
    ):
        key, url = match.group(1), match.group(2).rstrip(").,")
        doc_id = _index_or_id(key, docs)
        if doc_id:
            manuals[doc_id] = url

    if re.search(r"все\s+обязательн", text, re.I):
        ids = [d["id"] for d in docs if int(d.get("priority") or 2) == 1]
        return ids, manuals

    numbers = [int(n) for n in re.findall(r"\b(\d{1,2})\b", text)]
    ids_from_n = []
    for n in numbers:
        if 1 <= n <= len(docs):
            ids_from_n.append(docs[n - 1]["id"])
    hex_ids = [h.lower() for h in HEX_ID_RE.findall(text)]
    known = {d["id"] for d in docs}
    ids_from_hex = [h for h in hex_ids if h in known]
    merged = list(dict.fromkeys(ids_from_n + ids_from_hex + list(manuals.keys())))
    return merged, manuals


def _index_or_id(key: str, docs: list[dict]) -> Optional[str]:
    if key.isdigit():
        n = int(key)
        if 1 <= n <= len(docs):
            return docs[n - 1]["id"]
        return None
    known = {d["id"] for d in docs}
    return key.lower() if key.lower() in known else None


def _priority_label(p: Any) -> str:
    try:
        n = int(p)
    except (TypeError, ValueError):
        n = 2
    return {1: "обязательно", 2: "желательно", 3: "опционально"}.get(n, str(p))


def _format_docs(docs: list[dict]) -> str:
    if not docs:
        return "_Список пуст._"
    lines = []
    for i, doc in enumerate(docs, start=1):
        lines.append(
            f"{i}. **{doc.get('title')}** — {_priority_label(doc.get('priority'))}\n"
            f"   `{doc.get('id')}` · {doc.get('doc_type') or ''}\n"
            f"   {doc.get('why_needed') or ''}"
        )
    return "\n".join(lines)


def _format_case(state: dict) -> str:
    docs = state.get("documents") or []
    selected = sum(1 for d in docs if d.get("selected"))
    ok = sum(1 for d in docs if d.get("download_status") == "ok")
    return (
        f"Кейс `{state.get('case_id')}` · {state.get('status')}\n"
        f"{state.get('inspection_name')}\n"
        f"Документов: {len(docs)}, утверждено: {selected}, скачано: {ok}"
    )


async def _status(emitter: Emitter, description: str, done: bool = False) -> None:
    if not emitter:
        return
    await emitter({"type": "status", "data": {"description": description, "done": done}})


async def _req(
    method: str,
    url: str,
    timeout: float,
    json: Optional[dict] = None,
) -> Any:
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.request(method, url, json=json)
        if response.status_code >= 400:
            detail = response.text[:400]
            try:
                payload = response.json()
                detail = str(payload.get("detail") or payload)[:400]
            except Exception:
                pass
            raise RuntimeError(f"{method} {url} → {response.status_code}: {detail}")
        if not response.content:
            return {}
        return response.json()
