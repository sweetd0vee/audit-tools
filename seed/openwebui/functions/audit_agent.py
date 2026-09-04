"""
title: Аудитор
author: audit-tools
version: 0.0.1
license: MIT
description: Агент проверки. Документы, саммари, total, программа, гипотезы, мнение, заключение. Свои файлы — «загрузи»; вопрос по базе — «вопрос …»; иначе обычный чат.
requirements: httpx
"""

from __future__ import annotations

import base64
import json
import re
import time
from typing import Any, Awaitable, Callable, Optional

import httpx
from pydantic import BaseModel, Field

# INTENT_INLINE_START
# Локально / в pytest: соседний intent.py. Open WebUI принимает один файл —
# seed_pipe.py подменяет этот блок телом intent.py.
import sys
from pathlib import Path as _IntentDir

_intent_dir = str(_IntentDir(__file__).resolve().parent)
if _intent_dir not in sys.path:
    sys.path.insert(0, _intent_dir)
from intent import (  # noqa: E402
    Cmd,
    URL_ATTACH_RE,
    classify,
    _has_explicit_picks,
    _is_retry,
    _parse_hypothesis_picks,
    _parse_kb_question,
    _parse_new_case,
    _parse_opinion_font,
    _parse_program_items_spec,
    _resolve_approval,
    _wants_extra_hypotheses,
    _wants_opinion_after_select,
)
# INTENT_INLINE_END

Emitter = Optional[Callable[[Any], Awaitable[None]]]

CASE_MARK = re.compile(r"<!--audit-case:([a-z0-9]+)-->")
# Канонический текст: docs/prompts/pipe_help.txt. После правки скопируйте сюда и заново вставьте Pipe в Open WebUI.
NEXT_STEPS = (
    "— `программа проверки` — программа проверки в Word (можно задать число пунктов);\n"
    "— `саммари` — основная информация по теме из базы знаний в Word;\n"
    "— `саммари total` — основная информация по теме из знаний модели;\n"
    "— `гипотезы` — чеклист гипотез для проверки в Excel;\n"
    "— `аудиторское мнение` — черновик раздела I в Word после `утверждаю гипотезы …` (`-c` Calibri, `-t` Times New Roman). Свои гипотезы: `утверждаю гипотезы 1, 2 плюс формулировка` или .xlsx;\n"
    "— `аудиторское заключение` — черновик заключения в Word после `аудиторское мнение` (`-c` Calibri, `-t` Times New Roman);\n"
    "— `вопрос` — вопрос по базе знаний;\n"
    "— `документы` — посмотреть список документов в базе знаний;\n"
    "— `загрузи` — свои PDF/DOCX/TXT в базу (скрепка или папка inbox);\n"
    "— обычный диалог — пишите без префикса."
)
NO_CASE = "В этом чате ещё нет проверки. Сначала напишите, что проверяете."
HELP = f"""Я помогаю собрать документы для проверки и отвечать по ним.

Напишите, что проверяете, например:
Проверка аренды коммерческой недвижимости, аренда, валюта, НДС

Дальше я предложу список документов. Напишите, какие взять:
утверждаю 1, 2, 4
или: утверждаю все обязательные

Нет нужного акта в списке — допишите название:
утверждаю 1, 2 плюс Инструкция НБРБ № 38; Положение о внутреннем контроле
или отдельно: добавь Инструкция о порядке проведения валютных операций

Свои документы, которых нет в открытом доступе (внутренние положения, PDF с диска):
приложите файл скрепкой и напишите `загрузи`
или положите файлы в папку inbox проверки и напишите `загрузи`

Когда документы скачаются:
{NEXT_STEPS}
"""


_SIMPLE_ARTIFACTS = {
    "brief": {
        "endpoint": "brief",
        "start_message": (
            "Готовлю карточки существенного по актам в Word. "
            "Это может занять несколько минут…"
        ),
        "fallback_status": "Готовлю саммари…",
        "error_label": "обзор",
        "retry_hint": "Сначала должны быть скачаны документы. Напишите `документы` или `утверждаю 1, 2`.",
        "empty_message": "Обзор не получился. Напишите ещё раз: `саммари`.",
        "link_flags": {"with_summary": True},
    },
    "total": {
        "endpoint": "total",
        "start_message": (
            "Готовлю саммари total — конспект из знаний модели (не из базы). "
            "Это может занять несколько минут…"
        ),
        "fallback_status": "Готовлю саммари total…",
        "error_label": "саммари total",
        "retry_hint": "Нужна созданная проверка в этом чате. Напишите тему проверки, если кейса ещё нет.",
        "empty_message": "Саммари total не получился. Напишите ещё раз: `саммари total`.",
        "link_flags": {"with_total": True},
    },
    "hypotheses": {
        "endpoint": "hypotheses",
        "start_message": (
            "Формулирую чеклист гипотез для проверки в Excel. "
            "Лучше, если уже есть саммари / total / программа…"
        ),
        "fallback_status": "Формулирую гипотезы…",
        "error_label": "гипотезы",
        "retry_hint": (
            "Нужна проверка с документами. "
            "Желательно сначала `саммари`, `саммари total`, `программа проверки`."
        ),
        "empty_message": "Гипотезы не получились. Напишите ещё раз: `гипотезы`.",
        "link_flags": {"with_hypotheses": True},
    },
}

_FONT_ARTIFACTS = {
    "opinion": {
        "endpoint": "opinion",
        "start": (
            "Готовлю раздел I аудиторского заключения в Word ({font}). "
            "Это может занять несколько минут…"
        ),
        "fallback_status": "Готовлю аудиторское мнение…",
        "error_label": "аудиторское мнение",
        "retry_hint": (
            "Сначала `гипотезы`, затем `утверждаю гипотезы 1, 3, 5`. "
            "Свои: `утверждаю гипотезы 1, 2 плюс формулировка` или приложите .xlsx. "
            "Шрифт: `аудиторское мнение -c` (Calibri) или `-t` (Times New Roman)."
        ),
        "empty_message": "Аудиторское мнение не получилось. Напишите ещё раз: `аудиторское мнение`.",
        "link_flags": {"with_opinion": True},
    },
    "conclusion": {
        "endpoint": "conclusion",
        "start": (
            "Готовлю аудиторское заключение (черновик) в Word ({font}). "
            "Это может занять несколько минут…"
        ),
        "fallback_status": "Готовлю аудиторское заключение…",
        "error_label": "аудиторское заключение",
        "retry_hint": (
            "Сначала `гипотезы`, `утверждаю гипотезы 1, 3, 5` "
            "(свои — `плюс формулировка` или .xlsx), "
            "затем `аудиторское мнение`. "
            "Шрифт: `аудиторское заключение -c` (Calibri) или `-t` (Times New Roman)."
        ),
        "empty_message": (
            "Аудиторское заключение не получилось. "
            "Напишите ещё раз: `аудиторское заключение`."
        ),
        "link_flags": {"with_conclusion": True},
    },
}


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
            description="Таймаут сборки саммари, программы проверки и гипотез",
        )
        OPENWEBUI_API_KEY: str = Field(
            default="",
            description="Ключ Open WebUI (Settings → Account → API Keys). Пусто = коллекция Knowledge не создаётся, ответы идут через индекс сервера.",
        )

    def __init__(self) -> None:
        self.name = "Аудитор"
        self.valves = self.Valves()

    async def pipe(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __request__: Any = None,
        __event_emitter__: Emitter = None,
        __files__: Optional[list] = None,
        __metadata__: Optional[dict] = None,
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
        files_kw = __files__ if __files__ is not None else kwargs.get("__files__")
        metadata = __metadata__ if __metadata__ is not None else kwargs.get("__metadata__")

        command = classify(text, has_case=bool(case_id))
        attached = _knowledge_attachment_names(body, files_kw, metadata)
        if attached and command == Cmd.CHAT:
            command = Cmd.UPLOAD
        if attached and command == Cmd.HELP and not (text or "").strip():
            command = Cmd.UPLOAD
        if command == Cmd.HELP:
            return HELP

        await _status(__event_emitter__, "Смотрю, на каком вы шаге…")

        try:
            # «вопрос …» раньше library/brief: иначе «в данном документе» / «саммари»
            # внутри текста уводило в чужую ветку. Порядок веток — в classify().
            if command == Cmd.ASK:
                if not case_id:
                    return (
                        "В этом чате ещё нет проверки. Сначала напишите, что проверяете, "
                        "утвердите документы — потом: `вопрос …`."
                    )
                kb_question = _parse_kb_question(text) or ""
                if not kb_question:
                    return (
                        "Напишите вопрос после слова `вопрос`, например:\n"
                        "`вопрос Какой срок регистрации договора аренды?`\n"
                        f"<!--audit-case:{case_id}-->"
                    )
                return await self._ask(
                    api, timeout, case_id, kb_question, __event_emitter__
                )

            if command == Cmd.SELECT_HYPOTHESES:
                missing = _need_case(case_id)
                if missing:
                    return missing
                assert case_id is not None
                selected, confirmed = await self._select_hypotheses(
                    api,
                    timeout,
                    case_id,
                    text,
                    body,
                    __request__,
                    owui_key,
                    files_kw,
                    metadata,
                )
                if _wants_opinion_after_select(text) and confirmed:
                    opinion = await self._opinion(
                        api,
                        public,
                        self._brief_timeout(timeout),
                        case_id,
                        text,
                        __event_emitter__,
                    )
                    head = selected.rsplit("Дальше:", 1)[0].rstrip()
                    return f"{head}\n\n{opinion}"
                return selected

            artifacts = {
                Cmd.OPINION: self._opinion,
                Cmd.CONCLUSION: self._conclusion,
                Cmd.PROGRAM: self._program,
                Cmd.TOTAL: self._total,
                Cmd.HYPOTHESES: self._hypotheses,
                Cmd.BRIEF: self._brief,
            }
            handler = artifacts.get(command)
            if handler:
                return await self._dispatch_artifact(
                    handler, api, public, timeout, case_id, text, __event_emitter__
                )

            if command == Cmd.APPROVE:
                missing = _need_case(case_id)
                if missing:
                    return missing
                assert case_id is not None
                return await self._approve(
                    api,
                    public,
                    timeout,
                    case_id,
                    text,
                    __event_emitter__,
                    owui_key,
                    body,
                    __request__,
                    files_kw,
                    metadata,
                )

            if command == Cmd.UPLOAD:
                missing = _need_case(case_id)
                if missing:
                    return missing
                assert case_id is not None
                return await self._upload(
                    api,
                    public,
                    timeout,
                    case_id,
                    body,
                    __request__,
                    owui_key,
                    files_kw,
                    metadata,
                    __event_emitter__,
                )

            if command == Cmd.LIBRARY:
                missing = _need_case(case_id)
                if missing:
                    return missing
                assert case_id is not None
                return await self._library(api, public, timeout, case_id)

            if command == Cmd.STATUS:
                if case_id:
                    return await self._status_case(api, timeout, case_id)
                return await self._list_cases(api, timeout)

            if command == Cmd.NEW_CASE:
                parsed = _parse_new_case(text)
                if parsed:
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
            "Свои документы, которых нет в открытом доступе: приложите PDF/DOCX/TXT скрепкой и напишите `загрузи`.",
            "Или положите файлы в папку inbox проверки — путь скажу по команде `документы`.",
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
        body: Optional[dict] = None,
        request: Any = None,
        files_kw: Any = None,
        metadata: Any = None,
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
            attachments = await _knowledge_attachments(
                body or {},
                files_kw,
                request,
                owui_key,
                timeout,
                metadata=metadata,
            )
            if attachments:
                await _status(emitter, "Добавляю ваши файлы в базу знаний…")
                indexed = await _upload_knowledge_files(
                    api, timeout, case_id, attachments
                )
            else:
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
            "Свои внутренние акты, которых нет на pravo.by: приложите файл и напишите `загрузи` "
            "или положите в папку inbox (путь — команда `документы`).\n\n"
            f"{_download_links(public, case_id, name, with_summary=False)}\n\n"
            f"Дальше:\n{NEXT_STEPS}\n"
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
        if result.get("refused"):
            return (
                f"{(result.get('answer') or '').strip()}\n\n"
                "**Откуда в базе знаний:** отказ — подходящих фрагментов нет, "
                "номер статьи из памяти модели не подставляется.\n"
                f"<!--audit-case:{case_id}-->"
            )
        cites = []
        for s in sources[:6]:
            title = s.get("title") or s.get("filename") or "документ"
            article = (s.get("article") or "").strip()
            label = f"{title} — {article}" if article else title
            excerpt = (s.get("excerpt") or "").replace("\n", " ").strip()
            if len(excerpt) > 220:
                excerpt = excerpt[:220] + "…"
            cites.append(f"- {label}: {excerpt}")
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
        extra_query: Optional[dict[str, Any]] = None,
    ) -> str:
        force = bool(re.search(r"заново|пересобер|перегенер|force", text, re.I))
        await _status(emitter, start_message)
        result: dict[str, Any] | None = None
        url = f"{api}/api/v1/cases/{case_id}/knowledge/{endpoint}/stream"
        params: list[str] = []
        if force:
            params.append("force=true")
        for key, value in (extra_query or {}).items():
            if value is None or value == "":
                continue
            params.append(f"{key}={value}")
        if params:
            url += "?" + "&".join(params)
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
                            await _status(
                                emitter,
                                _with_elapsed(
                                    event.get("message") or fallback_status,
                                    event.get("elapsed_ms"),
                                ),
                            )
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
        parts = []
        footer = _elapsed_footer(result)
        if footer:
            parts.append(footer)
        parts.append(_download_links(public, case_id, name, with_archive=False, **link_flags))
        parts.append(f"<!--audit-case:{case_id}-->")
        if link_flags.get("with_conclusion") and emitter:
            await _attach_conclusion_docx(api, case_id, name, timeout, emitter)
        return "\n".join(parts)

    def _brief_timeout(self, timeout: float) -> float:
        return max(timeout, float(self.valves.BRIEF_TIMEOUT_SEC))

    async def _dispatch_artifact(
        self,
        handler,
        api: str,
        public: str,
        timeout: float,
        case_id: Optional[str],
        text: str,
        emitter: Emitter,
    ) -> str:
        missing = _need_case(case_id)
        if missing:
            return missing
        assert case_id is not None
        return await handler(
            api, public, self._brief_timeout(timeout), case_id, text, emitter
        )

    async def _simple_artifact(
        self,
        kind: str,
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
            **_SIMPLE_ARTIFACTS[kind],
        )

    async def _font_artifact(
        self,
        kind: str,
        api: str,
        public: str,
        timeout: float,
        case_id: str,
        text: str,
        emitter: Emitter,
    ) -> str:
        spec = _FONT_ARTIFACTS[kind]
        font = _parse_opinion_font(text)
        font_name = "Calibri" if font == "c" else "Times New Roman"
        return await self._stream_build(
            api,
            public,
            timeout,
            case_id,
            text,
            emitter,
            endpoint=spec["endpoint"],
            start_message=spec["start"].format(font=font_name),
            fallback_status=spec["fallback_status"],
            error_label=spec["error_label"],
            retry_hint=spec["retry_hint"],
            empty_message=spec["empty_message"],
            link_flags=spec["link_flags"],
            extra_query={"font": font},
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
        return await self._simple_artifact(
            "brief", api, public, timeout, case_id, text, emitter
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
        return await self._simple_artifact(
            "total", api, public, timeout, case_id, text, emitter
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
        items = _parse_program_items_spec(text)
        if items:
            lo, hi = items
            count = f"{lo} вопросов" if lo == hi else f"{lo}–{hi} вопросов"
            start_message = (
                f"Готовлю программу аудиторской проверки в Word ({count}). "
                "Это может занять несколько минут…"
            )
        else:
            start_message = (
                "Готовлю программу аудиторской проверки в Word. "
                "Это может занять несколько минут…"
            )
        extra = {}
        if items:
            extra["items"] = f"{items[0]}-{items[1]}" if items[0] != items[1] else str(items[0])
        return await self._stream_build(
            api,
            public,
            timeout,
            case_id,
            text,
            emitter,
            endpoint="program",
            start_message=start_message,
            fallback_status="Готовлю программу проверки…",
            error_label="программу проверки",
            retry_hint="Сначала должна быть создана проверка. Лучше, если акты уже скачаны: напишите `документы` или `утверждаю 1, 2`.",
            empty_message="Программа проверки не получилась. Напишите ещё раз: `программа проверки` или `программа проверки 10-12`.",
            link_flags={"with_program": True},
            extra_query=extra,
        )

    async def _hypotheses(
        self,
        api: str,
        public: str,
        timeout: float,
        case_id: str,
        text: str,
        emitter: Emitter,
    ) -> str:
        return await self._simple_artifact(
            "hypotheses", api, public, timeout, case_id, text, emitter
        )

    async def _select_hypotheses(
        self,
        api: str,
        timeout: float,
        case_id: str,
        text: str,
        body: dict,
        request: Any,
        token: str,
        files_kw: Any = None,
        metadata: Any = None,
    ) -> tuple[str, bool]:
        try:
            status = await _req(
                "GET",
                f"{api}/api/v1/cases/{case_id}/knowledge/hypotheses",
                timeout,
            )
        except Exception as exc:  # noqa: BLE001
            return (
                f"Не получилось прочитать чеклист гипотез: {exc}\n"
                "Сначала напишите `гипотезы`.\n"
                f"<!--audit-case:{case_id}-->"
            ), False
        if not status.get("ready"):
            return (
                "Чеклиста гипотез ещё нет. Сначала напишите `гипотезы`, "
                "затем: `утверждаю гипотезы 1, 3, 5`.\n"
                "Свои гипотезы — после `плюс` в том же сообщении или .xlsx.\n"
                f"<!--audit-case:{case_id}-->"
            ), False
        picks = _parse_hypothesis_picks(text)
        extra_rows = [
            row
            for row in (picks.get("extra_hypotheses") or [])
            if isinstance(row, dict) and (row.get("hypothesis") or "").strip()
        ]
        has_picks = bool(
            picks.get("numbers") or picks.get("all_high") or picks.get("all_rows")
        )
        wants_extra = _wants_extra_hypotheses(text) or bool(extra_rows)
        listed = _iter_attached_files(body, files_kw, metadata)
        attachments = await _xlsx_attachments(
            body,
            files_kw,
            request,
            token,
            timeout,
            metadata=metadata,
            allow_unknown=True,
        )
        if wants_extra and not attachments and not extra_rows:
            names = [
                _file_name(item) or _file_id(item) or "файл" for item in listed
            ]
            if listed:
                hint = (
                    f"Вижу вложение ({', '.join(names)}), но не смог прочитать Excel. "
                    "Нужен исходный .xlsx с колонкой «Гипотеза», не преобразованный отчёт. "
                    "Либо допишите формулировку после `плюс`.\n"
                )
            else:
                hint = (
                    "Свои гипотезы: допишите формулировку после `плюс` "
                    "(несколько — через `;`) или приложите .xlsx.\n"
                    "Пример: `утверждаю гипотезы 1, 2, 3 плюс "
                    "Курсовые разницы не пересчитываются ежемесячно`.\n"
                )
            if not has_picks and not listed:
                hint = (
                    "Приложите Excel со своими гипотезами к сообщению "
                    "`утверждаю гипотезы 1, 2, 3, 4` "
                    "или напишите: `добавить гипотезы плюс …формулировка…`.\n"
                )
            return (f"{hint}<!--audit-case:{case_id}-->"), False
        if not has_picks and not attachments and not extra_rows:
            return (
                "Укажите номера гипотез, которые подтвердились на проверке, например:\n"
                "`утверждаю гипотезы 1, 3, 5`\n"
                "или: `утверждаю гипотезы все с приоритетом высокий`\n"
                "или: `утверждаю все гипотезы`.\n"
                "Свои: `утверждаю гипотезы 1, 2 плюс формулировка` "
                "или приложите .xlsx (колонка «Гипотеза»).\n"
                f"<!--audit-case:{case_id}-->"
            ), False
        payload = {
            "numbers": picks.get("numbers") or [],
            "all_high": bool(picks.get("all_high")),
            "all_rows": bool(picks.get("all_rows")),
        }
        if not has_picks:
            payload["keep_numbers"] = True
        if extra_rows:
            payload["extra_hypotheses"] = extra_rows
        try:
            if attachments:
                name, raw = attachments[0]
                form = {
                    "numbers": json.dumps(payload.get("numbers") or []),
                    "all_high": str(bool(payload.get("all_high"))).lower(),
                    "all_rows": str(bool(payload.get("all_rows"))).lower(),
                    "keep_numbers": str(bool(payload.get("keep_numbers"))).lower(),
                }
                if extra_rows:
                    form["extra_hypotheses"] = json.dumps(extra_rows, ensure_ascii=False)
                data = await _req(
                    "POST",
                    f"{api}/api/v1/cases/{case_id}/knowledge/hypotheses/select",
                    timeout,
                    data=form,
                    files={
                        "extra": (
                            name,
                            raw,
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        )
                    },
                )
            else:
                data = await _req(
                    "POST",
                    f"{api}/api/v1/cases/{case_id}/knowledge/hypotheses/select",
                    timeout,
                    json=payload,
                )
        except Exception as extra:  # noqa: BLE001
            return (
                f"Не получилось сохранить выбор гипотез: {extra}\n"
                f"<!--audit-case:{case_id}-->"
            ), False
        rows = data.get("hypotheses") or []
        extra_count = int(data.get("extra_count") or 0)
        generated = int(data.get("count") or len(rows)) - extra_count
        if extra_count:
            head = (
                f"Подтвердил гипотезы: {generated} из чеклиста, "
                f"плюс {extra_count} ваших."
            )
        else:
            head = f"Подтвердил гипотезы: {data.get('count') or len(rows)}."
        lines = [
            head,
            "В аудиторское мнение и в наблюдения заключения пойдут все они.",
            "",
        ]
        for row in rows:
            mark = " (ваша)" if (row.get("origin") or "") == "auditor" else ""
            lines.append(
                f"- {row.get('n')}. [{row.get('priority')}] {row.get('hypothesis')}{mark}"
            )
        lines.append("")
        if extra_count == 0:
            lines.append(
                "Свои гипотезы: `утверждаю гипотезы 1, 2 плюс формулировка` "
                "или `добавить гипотезы` и .xlsx."
            )
        lines.append(
            "Дальше: `аудиторское мнение`, затем `аудиторское заключение` "
            "(шрифт: `-c` Calibri или `-t` Times New Roman)."
        )
        lines.append(f"<!--audit-case:{case_id}-->")
        return "\n".join(lines), True

    async def _opinion(
        self,
        api: str,
        public: str,
        timeout: float,
        case_id: str,
        text: str,
        emitter: Emitter,
    ) -> str:
        return await self._font_artifact(
            "opinion", api, public, timeout, case_id, text, emitter
        )

    async def _conclusion(
        self,
        api: str,
        public: str,
        timeout: float,
        case_id: str,
        text: str,
        emitter: Emitter,
    ) -> str:
        return await self._font_artifact(
            "conclusion", api, public, timeout, case_id, text, emitter
        )

    async def _upload(
        self,
        api: str,
        public: str,
        timeout: float,
        case_id: str,
        body: dict,
        request: Any,
        token: str,
        files_kw: Any,
        metadata: Any,
        emitter: Emitter,
    ) -> str:
        await _status(emitter, "Добавляю ваши документы в базу знаний…")
        attachments = await _knowledge_attachments(
            body, files_kw, request, token, timeout, metadata=metadata
        )
        skipped = _skipped_attachment_names(body, files_kw, metadata)
        try:
            if attachments:
                result = await _upload_knowledge_files(
                    api, timeout, case_id, attachments
                )
            else:
                result = await _req(
                    "POST", f"{api}/api/v1/cases/{case_id}/knowledge/index", timeout
                )
        except Exception as exc:  # noqa: BLE001
            return (
                f"Не получилось загрузить документы: {exc}\n"
                f"<!--audit-case:{case_id}-->"
            )
        if token:
            await _status(emitter, "Добавляю документы в базу знаний чата…")
            try:
                await _req(
                    "POST",
                    f"{api}/api/v1/cases/{case_id}/knowledge/openwebui/sync",
                    timeout,
                    json={"api_key": token},
                )
            except Exception:
                pass
        added = result.get("added") or []
        errors = result.get("errors") or []
        items = result.get("items") or []
        inbox = (result.get("inbox_dir") or "").strip()
        uploaded = [it for it in items if (it.get("source") or "") == "uploaded"]
        lines = []
        if added:
            lines.append(f"Добавила в базу знаний: {len(added)} файл(ов).")
            for item in added:
                title = item.get("title") or item.get("filename") or "файл"
                status = item.get("extract_status") or ""
                mark = " — текст не извлечён" if status and status != "ok" else ""
                lines.append(f"- {title}{mark}")
        elif uploaded:
            lines.append(
                f"В базе знаний уже есть ваши файлы: {len(uploaded)}. Новых не добавилось."
            )
        else:
            lines.append("Новых файлов не нашла.")
            lines.append(
                "Приложите PDF, DOCX, TXT, HTML или RTF скрепкой к сообщению `загрузи`."
            )
            if inbox:
                lines.append(f"Или положите файлы в папку `{inbox}` и напишите `загрузи` ещё раз.")
        if skipped:
            lines.append("")
            lines.append(
                "Не беру в базу знаний: "
                + ", ".join(skipped)
                + ". Excel клиента сюда не кладём — для своих гипотез приложите .xlsx к `утверждаю гипотезы …`."
            )
        if errors:
            lines.append("")
            lines.append("Не загрузились:")
            for err in errors:
                lines.append(f"- {err.get('filename')}: {err.get('error')}")
        n_items = len(items)
        if n_items:
            lines.append("")
            lines.append(f"Всего в базе знаний: {n_items} документов.")
        name = result.get("inspection_name") or "proverka"
        lines.append("")
        lines.append(_download_links(public, case_id, name, with_summary=False))
        lines.append("")
        lines.append("Дальше:")
        lines.extend(NEXT_STEPS.splitlines())
        lines.append(f"<!--audit-case:{case_id}-->")
        return "\n".join(lines)

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
        uploaded = data.get("uploaded") or []
        if uploaded:
            lines.append("")
            lines.append("Ваши файлы (не из открытого доступа):")
            for item in uploaded:
                lines.append(f"- {item.get('title') or item.get('filename')}")
        inbox = (data.get("inbox_dir") or "").strip()
        if inbox:
            lines.append("")
            lines.append(
                f"Папка для своих документов: `{inbox}`. Положите PDF/DOCX/TXT и напишите `загрузи`."
            )
        lines.append("")
        lines.append(_download_links(public, case_id, name, with_summary=False))
        lines.append("")
        lines.append("Дальше:")
        lines.extend(NEXT_STEPS.splitlines())
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


def _need_case(case_id: Optional[str]) -> Optional[str]:
    if case_id:
        return None
    return NO_CASE


def _last_user_text(body: dict) -> str:
    for message in reversed(body.get("messages") or []):
        if message.get("role") != "user":
            continue
        return _message_text(message.get("content"))
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


def _format_elapsed(ms: Any) -> str:
    try:
        total = int(ms)
    except (TypeError, ValueError):
        return ""
    if total < 0:
        total = 0
    seconds = (total + 500) // 1000
    if seconds < 1:
        return ""
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours} ч")
    if minutes:
        parts.append(f"{minutes} мин")
    if secs or not parts:
        parts.append(f"{secs} с")
    return " ".join(parts)


def _with_elapsed(message: str, elapsed_ms: Any) -> str:
    label = _format_elapsed(elapsed_ms)
    if not label:
        return message
    return f"{message} · {label}"


def _elapsed_footer(result: dict[str, Any]) -> str:
    reused = bool(result.get("reused"))
    built = _format_elapsed(result.get("built_elapsed_ms"))
    current = _format_elapsed(result.get("elapsed_ms"))
    if reused:
        if built:
            return f"Файл уже был готов. В прошлый раз генерация заняла {built}."
        return "Файл уже был готов."
    if current:
        return f"Сгенерировано за {current}."
    return ""


def _file_stem(inspection_name: str) -> str:
    base = re.sub(r"[^\w\u0400-\u04FF\-]+", "_", inspection_name or "", flags=re.UNICODE)
    return base.strip("_")[:60] or "proverka"


async def _attach_conclusion_docx(
    api: str,
    case_id: str,
    inspection_name: str,
    timeout: float,
    emitter: Emitter,
) -> None:
    """Put the generated Word file on the chat message so an old Downloads copy is not opened."""
    if not emitter:
        return
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                f"{api}/api/v1/cases/{case_id}/knowledge/conclusion.docx"
            )
        if response.status_code >= 400 or not response.content:
            return
        name = f"{_file_stem(inspection_name)}_zakluchenie.docx"
        await emitter(
            {
                "type": "files",
                "data": {
                    "files": [
                        {
                            "type": "file",
                            "name": name,
                            "url": (
                                "data:application/vnd.openxmlformats-officedocument"
                                ".wordprocessingml.document;base64,"
                                + base64.b64encode(response.content).decode("ascii")
                            ),
                        }
                    ]
                },
            }
        )
    except Exception:
        return


def _download_links(
    public: str,
    case_id: str,
    inspection_name: str,
    *,
    with_archive: bool = True,
    with_summary: bool = False,
    with_total: bool = False,
    with_program: bool = False,
    with_hypotheses: bool = False,
    with_opinion: bool = False,
    with_conclusion: bool = False,
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
    if with_hypotheses:
        lines.append(
            f"- чеклист гипотез (`{stem}_gipotezy.xlsx`): {base}/knowledge/hypotheses.xlsx"
        )
    if with_opinion:
        lines.append(
            f"- аудиторское мнение (`{stem}_mnenie.docx`): {base}/knowledge/opinion.docx"
        )
    if with_conclusion:
        lines.append(
            f"- аудиторское заключение (`{stem}_zakluchenie.docx`): {base}/knowledge/conclusion.docx?t={int(time.time())}"
        )
    return "\n".join(lines)


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


KNOWLEDGE_EXTS = {".txt", ".md", ".html", ".htm", ".pdf", ".docx", ".rtf"}
MAX_ATTACH_BYTES = 32 * 1024 * 1024


async def _status(emitter: Emitter, description: str, done: bool = False) -> None:
    if not emitter:
        return
    await emitter({"type": "status", "data": {"description": description, "done": done}})


def _file_suffix(name: str) -> str:
    lower = (name or "").strip().lower()
    if "." not in lower:
        return ""
    return "." + lower.rsplit(".", 1)[-1]


def _is_xlsx_name(name: str) -> bool:
    lower = (name or "").strip().lower()
    return lower.endswith(".xlsx") or lower.endswith(".xlsm")


def _is_knowledge_name(name: str) -> bool:
    return _file_suffix(name) in KNOWLEDGE_EXTS


def _knowledge_mime(name: str) -> str:
    return {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".html": "text/html",
        ".htm": "text/html",
        ".rtf": "application/rtf",
    }.get(_file_suffix(name), "application/octet-stream")


def _knowledge_attachment_names(body: dict, files_kw: Any, metadata: Any) -> list[str]:
    names = []
    for item in _iter_attached_files(body, files_kw, metadata):
        name = _file_name(item)
        if _is_knowledge_name(name):
            names.append(name)
    return names


def _skipped_attachment_names(body: dict, files_kw: Any, metadata: Any) -> list[str]:
    names = []
    for item in _iter_attached_files(body, files_kw, metadata):
        name = _file_name(item) or "файл"
        if name and not _is_knowledge_name(name):
            names.append(name)
    return names


def _file_name(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    nested = item.get("file") if isinstance(item.get("file"), dict) else {}
    meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
    nested_meta = nested.get("meta") if isinstance(nested.get("meta"), dict) else {}
    return str(
        item.get("filename")
        or item.get("name")
        or nested.get("filename")
        or nested.get("name")
        or meta.get("name")
        or nested_meta.get("name")
        or ""
    )


def _xlsx_mime(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    nested = item.get("file") if isinstance(item.get("file"), dict) else {}
    blobs: list[str] = []
    for src in (
        item,
        nested,
        item.get("meta") if isinstance(item.get("meta"), dict) else {},
        nested.get("meta") if isinstance(nested.get("meta"), dict) else {},
    ):
        if not isinstance(src, dict):
            continue
        for key in ("content_type", "mime_type", "mime", "type"):
            value = src.get(key)
            if isinstance(value, str) and value.strip():
                blobs.append(value.lower())
    joined = " ".join(blobs)
    return "spreadsheet" in joined or "excel" in joined or "xlsx" in joined


def _inline_xlsx_bytes(item: Any) -> Optional[bytes]:
    if not isinstance(item, dict):
        return None
    for key in ("bytes", "content", "blob"):
        value = item.get(key)
        raw = _as_xlsx_bytes(value)
        if raw:
            return raw
    data = item.get("data")
    if isinstance(data, dict):
        raw = _inline_xlsx_bytes(data)
        if raw:
            return raw
        raw = _as_xlsx_bytes(data.get("content"))
        if raw:
            return raw
    nested = item.get("file")
    if isinstance(nested, dict):
        return _inline_xlsx_bytes(nested)
    return None


def _as_xlsx_bytes(value: Any) -> Optional[bytes]:
    if isinstance(value, (bytes, bytearray)) and bytes(value[:2]) == b"PK":
        return bytes(value)
    if isinstance(value, str) and len(value) > 80:
        try:
            raw = base64.b64decode(value, validate=False)
        except Exception:
            return None
        if raw[:2] == b"PK":
            return raw
    return None


def _file_id(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    nested = item.get("file") if isinstance(item.get("file"), dict) else {}
    for src in (item, nested):
        for key in ("id", "file_id"):
            value = src.get(key)
            if isinstance(value, str) and value.strip() and value.strip().lower() != "file":
                return value.strip()
    url = str(item.get("url") or nested.get("url") or "")
    match = re.search(r"/files/([^/?#]+)", url)
    return match.group(1) if match else ""


def _file_paths(item: Any) -> list[str]:
    if not isinstance(item, dict):
        return []
    nested = item.get("file") if isinstance(item.get("file"), dict) else {}
    meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
    nested_meta = nested.get("meta") if isinstance(nested.get("meta"), dict) else {}
    found: list[str] = []
    seen: set[str] = set()
    for src in (item, nested, meta, nested_meta):
        if not isinstance(src, dict):
            continue
        for key in ("path", "local_path"):
            value = src.get(key)
            if isinstance(value, str) and value.strip() and value.strip() not in seen:
                seen.add(value.strip())
                found.append(value.strip())
    file_id = _file_id(item)
    name = _file_name(item)
    if file_id and name:
        for root in (
            "/app/backend/data/uploads",
            "/app/backend/data/cache/uploads",
        ):
            candidate = f"{root}/{file_id}_{name}"
            if candidate not in seen:
                seen.add(candidate)
                found.append(candidate)
    return found


def _read_file_bytes(path: str, limit: int = MAX_ATTACH_BYTES) -> Optional[bytes]:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(limit + 1)
    except OSError:
        return None
    if not raw or len(raw) > limit:
        return None
    return raw


def _read_path_bytes(path: str) -> Optional[bytes]:
    raw = _read_file_bytes(path)
    return raw if raw and raw[:2] == b"PK" else None


def _owui_bases(request: Any) -> list[str]:
    bases: list[str] = []
    url = getattr(request, "base_url", None)
    if url:
        bases.append(str(url).rstrip("/"))
    headers = getattr(request, "headers", None)
    if headers:
        host = headers.get("x-forwarded-host") or headers.get("host")
        proto = headers.get("x-forwarded-proto") or "http"
        if host:
            bases.append(f"{proto}://{host}")
    bases.extend(["http://127.0.0.1:8080", "http://open-webui:8080"])
    out: list[str] = []
    seen: set[str] = set()
    for base in bases:
        key = base.rstrip("/")
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _iter_attached_files(
    body: dict,
    files_kw: Any,
    metadata: Any = None,
) -> list[dict]:
    items: list[dict] = []
    blobs: list[Any] = [files_kw]
    if isinstance(body, dict):
        blobs.append(body.get("files"))
        nested_meta = body.get("metadata")
        if isinstance(nested_meta, dict):
            blobs.append(nested_meta.get("files"))
    if isinstance(metadata, dict):
        blobs.append(metadata.get("files"))
    messages = (body or {}).get("messages") or []
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        attached = message.get("files")
        if isinstance(attached, list):
            blobs.insert(0, attached)
        break
    seen: set[str] = set()
    for blob in blobs:
        if not isinstance(blob, list):
            continue
        for item in blob:
            if not isinstance(item, dict):
                continue
            key = _file_id(item) or _file_name(item) or str(id(item))
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
    return items


async def _xlsx_attachments(
    body: dict,
    files_kw: Any,
    request: Any,
    token: str,
    timeout: float,
    *,
    metadata: Any = None,
    allow_unknown: bool = False,
) -> list[tuple[str, bytes]]:
    found: list[tuple[str, bytes]] = []
    for item in _iter_attached_files(body, files_kw, metadata):
        name = _file_name(item) or "auditor.xlsx"
        looks = (
            _is_xlsx_name(name)
            or any(_is_xlsx_name(path) for path in _file_paths(item))
            or _inline_xlsx_bytes(item)
            or _xlsx_mime(item)
        )
        if not looks and not allow_unknown:
            continue
        raw = _inline_xlsx_bytes(item)
        if raw is None:
            for path in _file_paths(item):
                raw = _read_path_bytes(path)
                if raw:
                    break
        if raw is None:
            file_id = _file_id(item)
            if file_id:
                raw = await _download_owui_file(file_id, request, token, timeout)
        if raw and raw[:2] == b"PK":
            found.append((name if _is_xlsx_name(name) else "auditor.xlsx", raw))
    return found


async def _download_owui_file(
    file_id: str,
    request: Any,
    token: str,
    timeout: float,
) -> Optional[bytes]:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    cookies = getattr(request, "cookies", None)
    cookie_header = ""
    if cookies:
        if hasattr(cookies, "items"):
            cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
        elif isinstance(cookies, dict):
            cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
        if cookie_header:
            headers["Cookie"] = cookie_header
    for base in _owui_bases(request):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                meta = await client.get(f"{base}/api/v1/files/{file_id}", headers=headers)
                if meta.status_code < 400:
                    payload = meta.json()
                    if isinstance(payload, dict):
                        for path in _file_paths(payload):
                            raw = _read_file_bytes(path)
                            if raw:
                                return raw
                response = await client.get(
                    f"{base}/api/v1/files/{file_id}/content",
                    headers=headers,
                )
            if response.status_code < 400 and response.content:
                return response.content
        except Exception:
            continue
    return None


async def _knowledge_attachments(
    body: dict,
    files_kw: Any,
    request: Any,
    token: str,
    timeout: float,
    *,
    metadata: Any = None,
) -> list[tuple[str, bytes]]:
    found: list[tuple[str, bytes]] = []
    for item in _iter_attached_files(body, files_kw, metadata):
        name = _file_name(item) or "document.bin"
        if not _is_knowledge_name(name):
            continue
        raw = _inline_file_bytes(item)
        if raw is None:
            for path in _file_paths(item):
                raw = _read_file_bytes(path)
                if raw:
                    break
        if raw is None:
            file_id = _file_id(item)
            if file_id:
                raw = await _download_owui_file(file_id, request, token, timeout)
        if raw:
            found.append((name, raw))
    return found


def _inline_file_bytes(item: Any) -> Optional[bytes]:
    if not isinstance(item, dict):
        return None
    for key in ("bytes", "content", "blob"):
        value = item.get(key)
        raw = _as_file_bytes(value)
        if raw:
            return raw
    data = item.get("data")
    if isinstance(data, dict):
        nested = _inline_file_bytes(data)
        if nested:
            return nested
        raw = _as_file_bytes(data.get("content"))
        if raw:
            return raw
    nested = item.get("file")
    if isinstance(nested, dict):
        return _inline_file_bytes(nested)
    return None


def _as_file_bytes(value: Any) -> Optional[bytes]:
    if isinstance(value, (bytes, bytearray)) and value:
        raw = bytes(value)
        return raw if len(raw) <= MAX_ATTACH_BYTES else None
    if isinstance(value, str) and len(value) > 40:
        try:
            raw = base64.b64decode(value, validate=False)
        except Exception:
            return None
        if raw and len(raw) <= MAX_ATTACH_BYTES:
            return raw
    return None


async def _upload_knowledge_files(
    api: str,
    timeout: float,
    case_id: str,
    attachments: list[tuple[str, bytes]],
) -> Any:
    files = [
        ("files", (name, raw, _knowledge_mime(name))) for name, raw in attachments
    ]
    return await _req(
        "POST",
        f"{api}/api/v1/cases/{case_id}/knowledge/upload",
        timeout,
        files=files,
    )


async def _req(
    method: str,
    url: str,
    timeout: float,
    json: Optional[dict] = None,
    data: Optional[dict] = None,
    files: Any = None,
) -> Any:
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.request(
            method, url, json=json, data=data, files=files
        )
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
