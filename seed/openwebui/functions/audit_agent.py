"""
title: Аудитор
author: audit-tools
version: 0.0.1
license: MIT
description: Агент проверки. Документы, саммари, total, программа, гипотезы, мнение, заключение. Вопрос по базе — «вопрос …»; иначе обычный чат.
requirements: httpx
"""

from __future__ import annotations

import json
import re
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
    _is_opinion,
    _is_retry,
    _parse_hypothesis_picks,
    _parse_kb_question,
    _parse_new_case,
    _parse_opinion_font,
    _parse_program_items_spec,
    _resolve_approval,
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
    "— `аудиторское мнение` — черновик раздела I в Word после `утверждаю гипотезы …` (`-c` Calibri, `-t` Times New Roman);\n"
    "— `аудиторское заключение` — черновик заключения в Word после `аудиторское мнение` (`-c` Calibri, `-t` Times New Roman);\n"
    "— `вопрос` — вопрос по базе знаний;\n"
    "— `документы` — посмотреть список документов в базе знаний;\n"
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
            "Сначала `гипотезы`, `утверждаю гипотезы 1, 3, 5`, затем `аудиторское мнение`. "
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

        command = classify(text, has_case=bool(case_id))
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
                    api, timeout, case_id, text
                )
                if _is_opinion(text) and confirmed:
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
                    api, public, timeout, case_id, text, __event_emitter__, owui_key
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
                f"<!--audit-case:{case_id}-->"
            ), False
        picks = _parse_hypothesis_picks(text)
        if (
            not picks.get("numbers")
            and not picks.get("all_high")
            and not picks.get("all_rows")
        ):
            return (
                "Укажите номера гипотез, которые подтвердились на проверке, например:\n"
                "`утверждаю гипотезы 1, 3, 5`\n"
                "или: `утверждаю гипотезы все с приоритетом высокий`\n"
                "или: `утверждаю все гипотезы`.\n"
                f"<!--audit-case:{case_id}-->"
            ), False
        try:
            data = await _req(
                "POST",
                f"{api}/api/v1/cases/{case_id}/knowledge/hypotheses/select",
                timeout,
                json=picks,
            )
        except Exception as extra:  # noqa: BLE001
            return (
                f"Не получилось сохранить выбор гипотез: {extra}\n"
                f"<!--audit-case:{case_id}-->"
            ), False
        rows = data.get("hypotheses") or []
        lines = [
            f"Подтвердил гипотезы: {data.get('count') or len(rows)}.",
            "В аудиторское мнение пойдут только они.",
            "",
        ]
        for row in rows:
            lines.append(
                f"- {row.get('n')}. [{row.get('priority')}] {row.get('hypothesis')}"
            )
        lines.append("")
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
            f"- аудиторское заключение (`{stem}_zakluchenie.docx`): {base}/knowledge/conclusion.docx"
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
