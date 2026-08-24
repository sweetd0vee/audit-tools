"""
title: Аудитор
author: audit-tools
version: 0.1.5
license: MIT
description: Агент проверки. Собирает документы, саммари Word, отвечает по базе знаний.
requirements: httpx
"""

from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable, Optional

import httpx
from pydantic import BaseModel, Field

Emitter = Optional[Callable[[Any], Awaitable[None]]]

CASE_MARK = re.compile(r"<!--audit-case:([a-z0-9]+)-->")
APPROVE_RE = re.compile(
    r"(утвержд\w*|подтвержд\w*|выбираю|скачивай|скачай|скачать|бери\s+(эти\s+)?акты)",
    re.I,
)
REJECT_APPROVE_RE = re.compile(r"\bне\s+утвержд", re.I)
HEX_ID_RE = re.compile(r"\b([a-f0-9]{8,12})\b", re.I)
HELP = """Я помогаю собрать документы для проверки и отвечать по ним.

Напишите, что проверяете, например:
Проверка аренды коммерческой недвижимости, аренда, валюта, НДС

Дальше я предложу список документов. Напишите, какие взять:
утверждаю 1, 2, 4
или: утверждаю все обязательные

Когда документы скачаются:
— задавайте вопросы по базе знаний (приложенным документам);
— напишите «саммари» — получите краткий обзор в Word.

Если в приложенных документах нет ответа — так и скажу.
Посмотреть, что скачалось: напишите «документы».
"""


class Pipe:
    class Valves(BaseModel):
        AUDIT_API: str = Field(
            default="http://backend:8100",
            description="Audit Tool Server. Compose: http://backend:8100. С хоста: http://localhost:8100",
        )
        PUBLIC_API: str = Field(
            default="http://localhost:8100",
            description="Ссылка для браузера аудитора (zip и JSON библиотеки)",
        )
        TIMEOUT_SEC: int = Field(default=300, description="Таймаут propose/download")
        BRIEF_TIMEOUT_SEC: int = Field(
            default=900,
            description="Таймаут сборки саммари Word (минуты на каждый акт)",
        )
        OPENWEBUI_API_KEY: str = Field(
            default="",
            description="Ключ Open WebUI (Settings → Account → API Keys). Пусто = коллекция Knowledge не создаётся, ответы идут через индекс сервера.",
        )

    def __init__(self) -> None:
        self.valves = self.Valves()

    async def pipe(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __request__: Any = None,
        __event_emitter__: Emitter = None,
        **kwargs,
    ) -> str:
        text = _last_user_text(body)
        case_id = _case_id_from_messages(body.get("messages") or [])
        api = self.valves.AUDIT_API.rstrip("/")
        public = (self.valves.PUBLIC_API or "http://localhost:8100").rstrip("/")
        timeout = float(self.valves.TIMEOUT_SEC)
        owui_key = (self.valves.OPENWEBUI_API_KEY or "").strip() or _session_token(
            __user__, __request__
        )

        if not text.strip() or text.strip().lower() in {"помощь", "help", "/help", "?"}:
            return HELP

        await _status(__event_emitter__, "Смотрю, на каком вы шаге…")

        try:
            if _is_brief(text):
                if not case_id:
                    return "В этом чате ещё нет проверки. Сначала напишите, что проверяете."
                return await self._brief(
                    api,
                    public,
                    max(timeout, float(self.valves.BRIEF_TIMEOUT_SEC)),
                    case_id,
                    text,
                    __event_emitter__,
                )

            if _is_approve(text):
                if not case_id:
                    return "В этом чате ещё нет проверки. Сначала напишите, что проверяете."
                return await self._approve(
                    api, public, timeout, case_id, text, __event_emitter__, owui_key
                )

            if _is_library(text):
                if not case_id:
                    return "В этом чате ещё нет проверки. Сначала напишите, что проверяете."
                return await self._library(api, public, timeout, case_id)

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
            tip = ""
            if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
                tip = " Сервер проверки не отвечает. Попробуйте ещё раз через минуту."
            elif isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
                tip = " Операция долгая. Подождите и повторите сообщение."
            return f"Не получилось связаться с сервером проверки.{tip}"
        except Exception as exc:  # noqa: BLE001
            return f"Не получилось выполнить шаг: {exc}"
        finally:
            await _status(__event_emitter__, "", done=True)

    async def _start(
        self,
        api: str,
        timeout: float,
        parsed: dict[str, Any],
        emitter: Emitter,
    ) -> str:
        await _status(emitter, "Создаю проверку…")
        created = await _req(
            "POST",
            f"{api}/api/v1/cases",
            timeout,
            json={
                "inspection_name": parsed["inspection_name"],
                "keywords": parsed["keywords"],
            },
        )
        case_id = created["case_id"]
        await _status(emitter, "Подбираю список документов. Это может занять несколько минут…")
        proposed = await _req("POST", f"{api}/api/v1/cases/{case_id}/propose", timeout)
        docs = proposed.get("documents") or []
        kws = ", ".join(parsed["keywords"]) or "не указаны"
        lines = [
            f"Проверка: {created.get('inspection_name')}",
            f"Ключевые слова: {kws}",
            "",
            "Предлагаю документы для базы знаний. Пока ничего не скачиваю — сначала выберите номера.",
            "",
            _format_docs(docs),
            "",
            "Напишите, какие взять, например: `утверждаю 1, 2, 4`",
            "Или: `утверждаю все обязательные`.",
            "Если знаете ссылку на документ: `к 3 url https://pravo.by/document/?guid=…` (вставьте адрес целиком, без многоточия).",
            f"<!--audit-case:{case_id}-->",
        ]
        return "\n".join(lines)

    async def _approve(
        self,
        api: str,
        public: str,
        timeout: float,
        case_id: str,
        text: str,
        emitter: Emitter,
        owui_key: str = "",
    ) -> str:
        state = await _req("GET", f"{api}/api/v1/cases/{case_id}", timeout)
        docs = state.get("documents") or []
        ids, manuals = _resolve_approval(text, docs)
        prev = [d["id"] for d in docs if d.get("selected")]
        retry_only = _is_retry(text) and not ids and not manuals
        url_only = bool(manuals) and not _has_explicit_picks(text)
        if retry_only:
            ids = list(prev)
        elif url_only:
            ids = list(dict.fromkeys(prev + list(manuals.keys())))
        if not ids:
            if URL_ATTACH_RE.search(text) and not manuals:
                return (
                    "Ссылка обрезана или с многоточием — так скачать нельзя. "
                    "Вставьте адрес как в браузере, целиком.\n"
                    f"<!--audit-case:{case_id}-->"
                )
            return (
                "Не поняла, какие документы взять. Напишите номера из списка, "
                "например: `утверждаю 1, 2`. Или: `утверждаю все обязательные`.\n"
                f"<!--audit-case:{case_id}-->"
            )
        await _status(emitter, "Сохраняю ваш выбор…")
        body: dict[str, Any] = {"document_ids": ids}
        if manuals:
            body["manual_urls"] = manuals
        await _req("POST", f"{api}/api/v1/cases/{case_id}/select", timeout, json=body)
        await _status(emitter, "Скачиваю выбранные документы…")
        downloaded = await _req("POST", f"{api}/api/v1/cases/{case_id}/download", timeout)
        await _status(emitter, "Готовлю базу знаний из скачанных документов…")
        n_items = 0
        try:
            indexed = await _req(
                "POST", f"{api}/api/v1/cases/{case_id}/knowledge/index", timeout
            )
            n_items = len(indexed.get("items") or [])
        except Exception:
            pass
        if owui_key:
            await _status(emitter, "Добавляю документы в базу знаний чата…")
            try:
                await _req(
                    "POST",
                    f"{api}/api/v1/cases/{case_id}/knowledge/openwebui/sync",
                    timeout,
                    json={"api_key": owui_key},
                )
            except Exception:
                pass
        ok = downloaded.get("downloaded", 0)
        failed = downloaded.get("failed", 0)
        fail_lines = []
        for d in downloaded.get("documents") or []:
            if d.get("selected") and d.get("download_status") not in {"ok", "skipped", None}:
                fail_lines.append(f"- {d.get('title')}")
        extra = ""
        if fail_lines:
            extra = (
                "\nНе удалось скачать:\n"
                + "\n".join(fail_lines)
                + "\nНапишите `скачай` — попробую ещё раз. "
                "Или пришлите полную ссылку: `к 3 url https://pravo.by/document/?guid=…`\n"
            )
        name = state.get("inspection_name") or "proverka"
        kb = f"В базе знаний {n_items} документов." if n_items else "База знаний подготовлена."
        return (
            f"Готово. Скачано документов: {ok}"
            + (f", не скачалось: {failed}" if failed else "")
            + f". {kb}\n"
            f"{extra}\n"
            f"{_download_links(public, case_id, name, with_summary=False)}\n\n"
            "Дальше можно задавать вопросы по базе знаний (приложенным документам).\n"
            "Чтобы получить краткий обзор в Word, напишите `саммари`.\n"
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
        await _status(emitter, "Ищу ответ в приложенных документах…")
        try:
            result = await _req(
                "POST",
                f"{api}/api/v1/cases/{case_id}/knowledge/ask",
                timeout,
                json={"question": question},
            )
        except Exception as exc:  # noqa: BLE001
            return (
                f"Пока не могу ответить по базе знаний: {exc}\n"
                "Сначала выберите документы (`утверждаю 1, 2`) и дождитесь скачивания.\n"
                f"<!--audit-case:{case_id}-->"
            )
        sources = result.get("sources") or []
        cites = []
        for s in sources[:6]:
            title = s.get("title") or s.get("filename") or "документ"
            excerpt = (s.get("excerpt") or "").replace("\n", " ").strip()
            if len(excerpt) > 220:
                excerpt = excerpt[:220] + "…"
            cites.append(f"- {title}: {excerpt}")
        cite_block = (
            "\n".join(cites)
            if cites
            else "В приложенных документах этого не нашлось — не опирайтесь на ответ как на факт из базы."
        )
        return (
            f"{result.get('answer', '').strip()}\n\n"
            f"**Откуда в базе знаний:**\n{cite_block}\n"
            f"<!--audit-case:{case_id}-->"
        )

    async def _brief(
        self,
        api: str,
        public: str,
        timeout: float,
        case_id: str,
        text: str,
        emitter: Emitter,
    ) -> str:
        force = bool(re.search(r"заново|пересобер|перегенер|force", text, re.I))
        await _status(emitter, "Готовлю обзор базы знаний в Word. Это может занять несколько минут…")
        result: dict[str, Any] | None = None
        url = f"{api}/api/v1/cases/{case_id}/knowledge/brief/stream"
        if force:
            url += "?force=true"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code >= 400:
                        detail = (await response.aread())[:400].decode("utf-8", "replace")
                        raise RuntimeError(f"{response.status_code}: {detail}")
                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        try:
                            event = json.loads(line[6:])
                        except json.JSONDecodeError:
                            continue
                        kind = event.get("type")
                        if kind == "status":
                            await _status(emitter, event.get("message") or "Готовлю обзор…")
                        elif kind == "error":
                            raise RuntimeError(event.get("message") or "brief error")
                        elif kind == "result":
                            result = event
        except Exception as exc:  # noqa: BLE001
            return (
                f"Не получилось собрать обзор: {exc}\n"
                "Сначала должны быть скачаны документы. Напишите `документы` или `утверждаю 1, 2`.\n"
                f"<!--audit-case:{case_id}-->"
            )
        if not result:
            return (
                "Обзор не получился. Напишите ещё раз: `саммари`.\n"
                f"<!--audit-case:{case_id}-->"
            )
        name = result.get("inspection_name") or ""
        if not name:
            try:
                state = await _req("GET", f"{api}/api/v1/cases/{case_id}", timeout)
                name = state.get("inspection_name") or "proverka"
            except Exception:
                name = "proverka"
        digest = "\n".join(result.get("digest") or [])
        digest_block = f"\nКратко по документам:\n{digest}\n" if digest else ""
        return (
            "Обзор базы знаний готов. Скачайте Word и читайте сами — в чат полный текст не копирую.\n\n"
            f"{_download_links(public, case_id, name, with_summary=True)}\n"
            f"{digest_block}\n"
            "Дальше можно задавать вопросы по базе знаний (приложенным документам).\n"
            "Собрать обзор заново: `саммари заново`.\n"
            f"<!--audit-case:{case_id}-->"
        )

    async def _library(self, api: str, public: str, timeout: float, case_id: str) -> str:
        data = await _req("GET", f"{api}/api/v1/cases/{case_id}/library", timeout)
        name = data.get("inspection_name") or "proverka"
        lines = [
            "Документы в базе знаний этой проверки:",
            "",
        ]
        for doc in data.get("documents") or []:
            if not doc.get("selected") and doc.get("download_status") in (None, "skipped"):
                continue
            status = "скачан" if doc.get("download_status") == "ok" else "не скачался"
            lines.append(f"- {doc.get('title')} — {status}")
        lines.append("")
        lines.append(_download_links(public, case_id, name, with_summary=False))
        lines.append("")
        lines.append("Дальше можно задавать вопросы по базе знаний (приложенным документам).")
        lines.append("Чтобы получить краткий обзор в Word, напишите `саммари`.")
        lines.append(f"<!--audit-case:{case_id}-->")
        return "\n".join(lines)

    async def _status_case(self, api: str, timeout: float, case_id: str) -> str:
        state = await _req("GET", f"{api}/api/v1/cases/{case_id}", timeout)
        return _format_case(state) + f"\n<!--audit-case:{case_id}-->"

    async def _list_cases(self, api: str, timeout: float) -> str:
        rows = await _req("GET", f"{api}/api/v1/cases", timeout)
        if not rows:
            return "Проверок пока нет. Напишите, что проверяете, чтобы начать."
        lines = ["Ваши проверки:"]
        for row in rows[:20]:
            lines.append(f"- {row.get('inspection_name')}")
        lines.append("\nЧтобы начать новую — напишите название проверки. Вопросы задавайте в чате той проверки, по которой уже собраны документы.")
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


def _session_token(user: Optional[dict], request: Any) -> str:
    if user:
        for key in ("token", "api_key", "jwt"):
            value = user.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    cookies = getattr(request, "cookies", None)
    if cookies:
        for name in ("token", "owui-token"):
            value = cookies.get(name) if hasattr(cookies, "get") else None
            if value:
                return str(value).strip()
    headers = getattr(request, "headers", None)
    if headers:
        auth = headers.get("authorization") or headers.get("Authorization")
        if auth and str(auth).lower().startswith("bearer "):
            return str(auth).split(" ", 1)[1].strip()
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


URL_ATTACH_RE = re.compile(
    r"(?:к|пункт|акт|номер|id)\s*([a-f0-9]{8,12}|\d{1,2})\s+(?:url|ссылка)\s+(https?://\S+)",
    re.I,
)


def _clean_url(url: str) -> str:
    cleaned = (url or "").strip().strip("`\"'<>").rstrip(").,;]")
    if cleaned.endswith("%60"):
        cleaned = cleaned[:-3]
    if "..." in cleaned or "…" in cleaned:
        return ""
    if not cleaned.lower().startswith(("http://", "https://")):
        return ""
    return cleaned


def _is_retry(text: str) -> bool:
    return bool(re.search(r"скач|ещё раз|еще раз|повтор", text, re.I))


def _has_explicit_picks(text: str) -> bool:
    if re.search(r"все\s+обязательн", text, re.I):
        return True
    return bool(re.search(r"утвержд\w*|подтвержд\w*|выбираю", text, re.I)) and bool(
        re.search(r"\b\d{1,2}\b", text)
    )


def _is_brief(text: str) -> bool:
    t = text.strip().lower()
    if t in {"саммари", "сводка", "бриф", "docx", "word", "/brief", "/summary"}:
        return True
    if re.search(r"(статья|ст\.)\s*\d+", t) and not re.search(r"\bdocx\b|word-файл", t):
        return False
    return bool(
        re.search(
            r"(саммари|сводк\w*|бриф|briefing|\bdocx\b|/brief|/summary|"
            r"обзор\s+(акт|нпа|норм)|word-файл|файл word)",
            t,
        )
    )


def _is_approve(text: str) -> bool:
    if REJECT_APPROVE_RE.search(text):
        return False
    if APPROVE_RE.search(text):
        return True
    return bool(URL_ATTACH_RE.search(text))


def _is_library(text: str) -> bool:
    t = text.strip().lower()
    if _is_brief(t):
        return False
    if re.search(r"скачай|скачать|скачивай", t):
        return False
    keys = (
        "документ",
        "библиотек",
        "скача",
        "файлы",
        "архив",
        "посмотреть акты",
        "покажи акты",
        "/library",
    )
    return any(k in t for k in keys)


def _file_stem(inspection_name: str) -> str:
    base = re.sub(r"[^\w\u0400-\u04FF\-]+", "_", inspection_name or "", flags=re.UNICODE)
    return base.strip("_")[:60] or "proverka"


def _download_links(
    public: str,
    case_id: str,
    inspection_name: str,
    *,
    with_summary: bool,
) -> str:
    stem = _file_stem(inspection_name)
    base = f"{public}/api/v1/cases/{case_id}"
    lines = [
        "Скачать:",
        f"- архив документов (`{stem}_npa.zip`): {base}/library/archive",
    ]
    if with_summary:
        lines.append(
            f"- обзор базы знаний (`{stem}_summary.docx`): {base}/knowledge/brief.docx"
        )
    return "\n".join(lines)


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
    if _is_approve(raw) or _is_status(raw) or _is_library(raw) or _is_brief(raw):
        return None
    parts = [p.strip(" .;") for p in re.split(r"[,;\n]", raw) if p.strip()]
    if not parts:
        return None
    name = parts[0]
    keywords = [part for part in parts[1:] if part]
    looks_like_case = bool(
        re.search(r"проверк|аудит|аренда|кредит|валют|касс|нпа", raw, re.I)
    ) or (len(name) >= 12 and not _looks_like_question(raw))
    if not looks_like_case:
        return None
    return {
        "inspection_name": name,
        "keywords": keywords,
    }


def _resolve_approval(text: str, docs: list[dict]) -> tuple[list[str], dict[str, str]]:
    manuals: dict[str, str] = {}
    for match in URL_ATTACH_RE.finditer(text):
        key, raw_url = match.group(1), match.group(2)
        url = _clean_url(raw_url)
        if not url:
            continue
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
            f"   {doc.get('why_needed') or ''}"
        )
    return "\n".join(lines)


def _format_case(state: dict) -> str:
    docs = state.get("documents") or []
    selected = sum(1 for d in docs if d.get("selected"))
    ok = sum(1 for d in docs if d.get("download_status") == "ok")
    return (
        f"Проверка: {state.get('inspection_name')}\n"
        f"В списке документов: {len(docs)}, выбрано: {selected}, скачано: {ok}"
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
