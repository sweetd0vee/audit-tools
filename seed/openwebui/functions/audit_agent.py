"""
title: Аудитор
author: audit-tools
version: 0.2.2
license: MIT
description: Агент проверки. Собирает документы, саммари, саммари total и программу. Вопрос по базе — с префиксом «вопрос»; иначе обычный чат с LLM.
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
EXTRA_MARK_RE = re.compile(
    r"(?:\bплюс\b|\bдобавь(?:те)?\b|\bдополнительно\b|\bи\s+ещ[её]\b|\bещ[её]\s*:|\s\+\s)\s*[:\-–+]?\s*",
    re.I,
)
KB_ASK_RE = re.compile(
    r"^\s*(?:"
    r"вопрос(?:\s+по\s+(?:базе(?:\s+знаний)?|нпа|документам?))?"
    r"|/ask|/вопрос"
    r")\s*[:\-–]?\s*(.*)\s*$",
    re.I | re.S,
)
# Канонический текст: docs/prompts/pipe_help.txt. После правки скопируйте сюда и заново вставьте Pipe в Open WebUI.
HELP = """Я помогаю собрать документы для проверки и отвечать по ним.

Напишите, что проверяете, например:
Проверка аренды коммерческой недвижимости, аренда, валюта, НДС

Дальше я предложу список документов. Напишите, какие взять:
утверждаю 1, 2, 4
или: утверждаю все обязательные

Нет нужного акта в списке — допишите название:
утверждаю 1, 2 плюс Инструкция НБРБ № 38; Положение о внутреннем контроле
или отдельно: добавь Инструкция о порядке проведения валютных операций

Когда документы скачаются:
— `программа проверки` — программа проверки в Word;
— `саммари` — основная информация по теме из базы знаний в Word;
— `саммари total` — основная информация по теме из знаний модели;
— `вопрос` — вопрос по базе знаний (например `вопрос Какой срок…`);
— `документы` — посмотреть список документов в базе знаний;
— обычный диалог — пишите без префикса.
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
        TIMEOUT_SEC: int = Field(default=600, description="Таймаут propose/download")
        BRIEF_TIMEOUT_SEC: int = Field(
            default=1800,
            description="Таймаут сборки саммари и программы проверки в Word",
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
            # «вопрос …» раньше library/brief: иначе «в данном документе» / «саммари»
            # внутри текста уводило в чужую ветку.
            kb_question = _parse_kb_question(text)
            if kb_question is not None:
                if not case_id:
                    return (
                        "В этом чате ещё нет проверки. Сначала напишите, что проверяете, "
                        "утвердите документы — потом: `вопрос …`."
                    )
                if not kb_question:
                    return (
                        "Напишите вопрос после слова `вопрос`, например:\n"
                        "`вопрос Какой срок регистрации договора аренды?`\n"
                        f"<!--audit-case:{case_id}-->"
                    )
                return await self._ask(
                    api, timeout, case_id, kb_question, __event_emitter__
                )

            if _is_program(text):
                if not case_id:
                    return "В этом чате ещё нет проверки. Сначала напишите, что проверяете."
                return await self._program(
                    api,
                    public,
                    max(timeout, float(self.valves.BRIEF_TIMEOUT_SEC)),
                    case_id,
                    text,
                    __event_emitter__,
                )

            if _is_total(text):
                if not case_id:
                    return "В этом чате ещё нет проверки. Сначала напишите, что проверяете."
                return await self._total(
                    api,
                    public,
                    max(timeout, float(self.valves.BRIEF_TIMEOUT_SEC)),
                    case_id,
                    text,
                    __event_emitter__,
                )

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
            if parsed and not case_id:
                return await self._start(api, timeout, parsed, __event_emitter__)

            return await self._chat(api, timeout, body, case_id, __event_emitter__)
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
        await _status(emitter, "Подбираю список документов (npa). Это может занять несколько минут…")
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
            "Нет в списке — допишите названия: `утверждаю 1, 2 плюс Инструкция НБРБ № 38; Положение о внутреннем контроле`.",
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
        ids, manuals, extras = _resolve_approval(text, docs)
        prev = [d["id"] for d in docs if d.get("selected")]
        retry_only = _is_retry(text) and not ids and not manuals and not extras
        url_only = bool(manuals) and not _has_explicit_picks(text) and not extras
        if retry_only:
            ids = list(prev)
        elif url_only:
            ids = list(dict.fromkeys(prev + list(manuals.keys())))
        elif extras and not ids:
            ids = list(prev)
        if not ids and not extras:
            if URL_ATTACH_RE.search(text) and not manuals:
                return (
                    "Ссылка обрезана или с многоточием — так скачать нельзя. "
                    "Вставьте адрес как в браузере, целиком.\n"
                    f"<!--audit-case:{case_id}-->"
                )
            return (
                "Не поняла, какие документы взять. Напишите номера из списка, "
                "например: `утверждаю 1, 2`. Или: `утверждаю все обязательные`.\n"
                "Нет нужного акта — допишите название: "
                "`утверждаю 1, 2 плюс Инструкция НБРБ № 38`.\n"
                f"<!--audit-case:{case_id}-->"
            )
        status = str(state.get("status") or "")
        selected_now = {d["id"] for d in docs if d.get("selected")}
        skip_select = (
            status == "downloading"
            and not extras
            and not manuals
            and bool(ids)
            and set(ids) <= selected_now
        )
        if skip_select:
            await _status(emitter, "Документы уже скачиваются, жду окончания…")
        else:
            await _status(emitter, "Сохраняю ваш выбор…")
            body: dict[str, Any] = {"document_ids": ids}
            if manuals:
                body["manual_urls"] = manuals
            if extras:
                body["extra_titles"] = extras
                await _status(emitter, "Ищу документы по вашим названиям…")
            try:
                await _req("POST", f"{api}/api/v1/cases/{case_id}/select", timeout, json=body)
            except RuntimeError as exc:
                if "downloading" not in str(exc).lower():
                    raise
                await _status(emitter, "Документы уже скачиваются, жду окончания…")
        await _status(
            emitter,
            "Скачиваю выбранные документы"
            + (" и те, что вы добавили по названию…" if extras else "…"),
        )
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
                err = (d.get("download_error") or "").replace("\n", " ").strip()
                if len(err) > 120:
                    err = err[:120] + "…"
                line = f"- {d.get('title')}"
                if err:
                    line += f" ({err})"
                fail_lines.append(line)
        extra = ""
        if fail_lines:
            extra = (
                "\nНе удалось скачать:\n"
                + "\n".join(fail_lines)
                + "\nНапишите `скачай` — попробую ещё раз. "
                "Или пришлите полную ссылку: `к 3 url https://pravo.by/document/?guid=…`\n"
            )
        added = ""
        if extras:
            added = (
                "Добавлены по вашему названию (ищу официальный текст):\n"
                + "\n".join(f"- {t}" for t in extras)
                + "\n"
            )
        name = state.get("inspection_name") or "proverka"
        kb = f"В базе знаний {n_items} документов." if n_items else "База знаний подготовлена."
        return (
            f"Готово. Скачано документов: {ok}"
            + (f", не скачалось: {failed}" if failed else "")
            + f". {kb}\n"
            f"{added}{extra}\n"
            f"{_download_links(public, case_id, name, with_summary=False)}\n\n"
            "Дальше:\n"
            "— `программа проверки` — программа проверки в Word;\n"
            "— `саммари` — основная информация по теме из базы знаний в Word;\n"
            "— `саммари total` — основная информация по теме из знаний модели;\n"
            "— `вопрос` — вопрос по базе знаний (например `вопрос Какой срок…`);\n"
            "— `документы` — посмотреть список документов в базе знаний;\n"
            "— обычный диалог — пишите без префикса.\n"
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

    async def _chat(
        self,
        api: str,
        timeout: float,
        body: dict,
        case_id: Optional[str],
        emitter: Emitter,
    ) -> str:
        await _status(emitter, "Отвечаю как в обычном чате…")
        messages = _messages_for_chat(body)
        if not messages:
            return HELP if not case_id else HELP + f"\n<!--audit-case:{case_id}-->"
        try:
            result = await _req(
                "POST",
                f"{api}/api/v1/chat",
                timeout,
                json={"messages": messages},
            )
        except Exception as exc:  # noqa: BLE001
            tip = f"\n<!--audit-case:{case_id}-->" if case_id else ""
            return f"Не получилось ответить в чате: {exc}{tip}"
        answer = (result.get("answer") or "").strip() or "Пустой ответ модели."
        if case_id:
            return f"{answer}\n<!--audit-case:{case_id}-->"
        return answer

    async def _stream_build(
        self,
        api: str,
        public: str,
        timeout: float,
        case_id: str,
        text: str,
        emitter: Emitter,
        *,
        endpoint: str,
        start_message: str,
        fallback_status: str,
        error_label: str,
        retry_hint: str,
        empty_message: str,
        link_flags: dict[str, bool],
    ) -> str:
        force = bool(re.search(r"заново|пересобер|перегенер|force", text, re.I))
        await _status(emitter, start_message)
        result: dict[str, Any] | None = None
        url = f"{api}/api/v1/cases/{case_id}/knowledge/{endpoint}/stream"
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
                            await _status(emitter, event.get("message") or fallback_status)
                        elif kind == "error":
                            raise RuntimeError(event.get("message") or f"{endpoint} error")
                        elif kind == "result":
                            result = event
        except Exception as exc:  # noqa: BLE001
            return (
                f"Не получилось собрать {error_label}: {exc}\n"
                f"{retry_hint}\n"
                f"<!--audit-case:{case_id}-->"
            )
        if not result:
            return (
                f"{empty_message}\n"
                f"<!--audit-case:{case_id}-->"
            )
        name = result.get("inspection_name") or ""
        if not name:
            try:
                state = await _req("GET", f"{api}/api/v1/cases/{case_id}", timeout)
                name = state.get("inspection_name") or "proverka"
            except Exception:
                name = "proverka"
        return (
            f"{_download_links(public, case_id, name, with_archive=False, **link_flags)}\n"
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
        return await self._stream_build(
            api,
            public,
            timeout,
            case_id,
            text,
            emitter,
            endpoint="brief",
            start_message="Готовлю карточки существенного по актам в Word. Это может занять несколько минут…",
            fallback_status="Готовлю саммари…",
            error_label="обзор",
            retry_hint="Сначала должны быть скачаны документы. Напишите `документы` или `утверждаю 1, 2`.",
            empty_message="Обзор не получился. Напишите ещё раз: `саммари`.",
            link_flags={"with_summary": True},
        )

    async def _total(
        self,
        api: str,
        public: str,
        timeout: float,
        case_id: str,
        text: str,
        emitter: Emitter,
    ) -> str:
        return await self._stream_build(
            api,
            public,
            timeout,
            case_id,
            text,
            emitter,
            endpoint="total",
            start_message="Готовлю саммари total — конспект из знаний модели (не из базы). Это может занять несколько минут…",
            fallback_status="Готовлю саммари total…",
            error_label="саммари total",
            retry_hint="Нужна созданная проверка в этом чате. Напишите тему проверки, если кейса ещё нет.",
            empty_message="Саммари total не получился. Напишите ещё раз: `саммари total`.",
            link_flags={"with_total": True},
        )

    async def _program(
        self,
        api: str,
        public: str,
        timeout: float,
        case_id: str,
        text: str,
        emitter: Emitter,
    ) -> str:
        return await self._stream_build(
            api,
            public,
            timeout,
            case_id,
            text,
            emitter,
            endpoint="program",
            start_message="Готовлю программу аудиторской проверки в Word. Это может занять несколько минут…",
            fallback_status="Готовлю программу проверки…",
            error_label="программу проверки",
            retry_hint="Сначала должна быть создана проверка. Лучше, если акты уже скачаны: напишите `документы` или `утверждаю 1, 2`.",
            empty_message="Программа проверки не получилась. Напишите ещё раз: `программа проверки`.",
            link_flags={"with_program": True},
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
        lines.append("Дальше:")
        lines.append("— `программа проверки` — программа проверки в Word;")
        lines.append("— `саммари` — основная информация по теме из базы знаний в Word;")
        lines.append("— `саммари total` — основная информация по теме из знаний модели;")
        lines.append("— `вопрос` — вопрос по базе знаний (например `вопрос Какой срок…`);")
        lines.append("— `документы` — посмотреть список документов в базе знаний;")
        lines.append("— обычный диалог — пишите без префикса.")
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


def _is_program(text: str) -> bool:
    t = text.strip().lower()
    if t in {"программа", "программу", "/program", "audit program"}:
        return True
    if re.search(r"(статья|ст\.)\s*\d+", t) and not re.search(r"программ", t):
        return False
    return bool(
        re.search(
            r"(программ\w*\s+(проверк|аудиторск|аудита)|"
            r"аудиторск\w*\s+программ|"
            r"(сделай|составь|подготовь|напиши)\s+программ|"
            r"/program|audit\s+program)",
            t,
        )
    )


def _is_total(text: str) -> bool:
    t = text.strip().lower()
    if t in {
        "саммари total",
        "саммари тотал",
        "total саммари",
        "/total",
        "конспект модели",
        "из головы",
    }:
        return True
    return bool(
        re.search(
            r"("
            r"саммари\s+total|"
            r"саммари\s+тотал|"
            r"total\s+саммари|"
            r"сводк\w*\s+total|"
            r"конспект\s+(модели|llm|из\s+голов)|"
            r"из\s+голов\w*\s+модел|"
            r"(обзор|конспект)\s+без\s+баз"
            r")",
            t,
        )
    )


def _is_brief(text: str) -> bool:
    t = text.strip().lower()
    if _is_total(t):
        return False
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
    if URL_ATTACH_RE.search(text):
        return True
    # Доп. акты по названию — только как реплика целиком, не «плюс» внутри названия проверки
    return bool(
        re.match(
            r"^\s*(добавь(?:те)?|дополнительно|и\s+ещ[её]|ещ[её]\s*:|\+)\b",
            text,
            re.I,
        )
    )


def _is_library(text: str) -> bool:
    """Команда списка/архива, не любой текст со словом «документ»."""
    t = text.strip().lower()
    if _is_brief(t) or _is_program(t) or _is_total(t):
        return False
    if re.search(r"скачай|скачать|скачивай", t):
        return False
    if t in {
        "документы",
        "документ",
        "библиотека",
        "библиотеку",
        "файлы",
        "архив",
        "/library",
    }:
        return True
    return bool(
        re.search(
            r"("
            r"посмотреть\s+(акты|документы)|"
            r"покажи\s+(акты|документы)|"
            r"что\s+скача|"
            r"список\s+документов|"
            r"/library"
            r")",
            t,
        )
    )


def _file_stem(inspection_name: str) -> str:
    base = re.sub(r"[^\w\u0400-\u04FF\-]+", "_", inspection_name or "", flags=re.UNICODE)
    return base.strip("_")[:60] or "proverka"


def _download_links(
    public: str,
    case_id: str,
    inspection_name: str,
    *,
    with_archive: bool = True,
    with_summary: bool = False,
    with_total: bool = False,
    with_program: bool = False,
) -> str:
    stem = _file_stem(inspection_name)
    base = f"{public}/api/v1/cases/{case_id}"
    lines = ["Скачать:"]
    if with_archive:
        lines.append(
            f"- архив документов (`{stem}_npa.zip`): {base}/library/archive"
        )
    if with_summary:
        lines.append(
            f"- обзор базы знаний (`{stem}_summary.docx`): {base}/knowledge/summary.docx"
        )
    if with_total:
        lines.append(
            f"- саммари total (`{stem}_total.docx`): {base}/knowledge/total.docx"
        )
    if with_program:
        lines.append(
            f"- программа проверки (`{stem}_programma.docx`): {base}/knowledge/program.docx"
        )
    return "\n".join(lines)


def _is_status(text: str) -> bool:
    t = text.strip().lower()
    return t in {"статус", "status", "кейсы", "проверки", "/status"} or t.startswith("статус ")


def _parse_kb_question(text: str) -> Optional[str]:
    """Явный вопрос к базе знаний: «вопрос …» / «вопрос по базе: …» / `/ask …`.

    Возвращает текст вопроса (может быть пустым), или None если это не команда ask.
    """
    match = KB_ASK_RE.match(text.strip())
    if not match:
        return None
    return (match.group(1) or "").strip()


def _message_text(content: Any) -> str:
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") in (None, "text"):
                parts.append(str(item.get("text") or ""))
        return "\n".join(parts).strip()
    return str(content or "").strip()


def _messages_for_chat(body: dict) -> list[dict[str, str]]:
    """История user/assistant для обычного чата без служебных меток кейса."""
    out: list[dict[str, str]] = []
    for message in body.get("messages") or []:
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        text = _message_text(message.get("content"))
        text = CASE_MARK.sub("", text).strip()
        text = re.sub(
            r"\n*\*\*Откуда в базе знаний:\*\*.*$",
            "",
            text,
            flags=re.S,
        ).strip()
        if not text:
            continue
        out.append({"role": role, "content": text})
    return out[-24:]


def _parse_new_case(text: str) -> Optional[dict[str, Any]]:
    raw = text.strip()
    if len(raw) < 8:
        return None
    if _parse_kb_question(raw) is not None:
        return None
    if (
        _is_approve(raw)
        or _is_status(raw)
        or _is_library(raw)
        or _is_brief(raw)
        or _is_total(raw)
        or _is_program(raw)
    ):
        return None
    parts = [p.strip(" .;") for p in re.split(r"[,;\n]", raw) if p.strip()]
    if not parts:
        return None
    name = parts[0]
    keywords = [part for part in parts[1:] if part]
    looks_like_case = bool(
        re.search(r"проверк|аудит|аренда|кредит|валют|касс|нпа", raw, re.I)
    ) or len(name) >= 12
    if not looks_like_case:
        return None
    return {
        "inspection_name": name,
        "keywords": keywords,
    }


def _resolve_approval(
    text: str, docs: list[dict]
) -> tuple[list[str], dict[str, str], list[str]]:
    main, extras_blob = _split_extra_section(text)
    extras = _parse_extra_titles(extras_blob)
    manuals: dict[str, str] = {}
    for match in URL_ATTACH_RE.finditer(text):
        key, raw_url = match.group(1), match.group(2)
        url = _clean_url(raw_url)
        if not url:
            continue
        doc_id = _index_or_id(key, docs)
        if doc_id:
            manuals[doc_id] = url

    if re.search(r"все\s+обязательн", main, re.I):
        ids = [d["id"] for d in docs if int(d.get("priority") or 2) == 1]
        return ids, manuals, extras

    numbers = [int(n) for n in re.findall(r"\b(\d{1,2})\b", main)]
    ids_from_n = []
    for n in numbers:
        if 1 <= n <= len(docs):
            ids_from_n.append(docs[n - 1]["id"])
    hex_ids = [h.lower() for h in HEX_ID_RE.findall(main)]
    known = {d["id"] for d in docs}
    ids_from_hex = [h for h in hex_ids if h in known]
    merged = list(dict.fromkeys(ids_from_n + ids_from_hex + list(manuals.keys())))
    return merged, manuals, extras


def _split_extra_section(text: str) -> tuple[str, str]:
    match = EXTRA_MARK_RE.search(text)
    if not match:
        return text, ""
    return text[: match.start()].strip(), text[match.end() :].strip()


def _parse_extra_titles(blob: str) -> list[str]:
    raw = (blob or "").strip().strip(" .,:;")
    if not raw:
        return []
    parts = re.split(r"\s*[;\n]\s*|\s+\+\s+", raw)
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        chunk = part.strip()
        and_split = re.search(r"\s+и\s+", chunk, re.I)
        pieces = [chunk]
        if and_split:
            right = chunk[and_split.end() :].strip()
            if re.match(
                r"^(закон|кодекс|инструкц|положен|постановлен|указ|декрет|"
                r"правил|налоговый|гражданский|банковский)",
                right,
                re.I,
            ):
                left = chunk[: and_split.start()].strip()
                pieces = [left, right] if left else [right]
        for piece in pieces:
            title = re.sub(r"^[\d]+[.)]\s*", "", piece).strip(" .,:;")
            title = re.sub(
                r"^(?:и|ещ[её]|также|плюс|добавь(?:те)?|документы?)\s+",
                "",
                title,
                flags=re.I,
            ).strip(" .,:;")
            key = re.sub(r"\s+", " ", title.lower())
            if len(key) < 8 or key in seen:
                continue
            if re.search(
                r"(audit-case|chat_history|если знаете ссылку|вставьте адрес|"
                r"<!--|</?\w+>|https?://|guid=…)",
                title,
                re.I,
            ):
                continue
            if not re.search(
                r"(закон|кодекс|инструкц|положен|постановлен|указ|декрет|"
                r"правил|приказ|письмо|разъяснен|нбрб|минфин|налогов)",
                title,
                re.I,
            ):
                continue
            seen.add(key)
            out.append(title)
    return out


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
