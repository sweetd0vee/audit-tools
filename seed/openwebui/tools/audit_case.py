"""
title: Audit Case Tools
author: audit-tools
version: 0.1.0
license: MIT
description: Руки агента — HTTP к Audit Tool Server. Без HITL не вызывай download.
requirements: httpx
"""

from __future__ import annotations

from typing import Optional

import httpx
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        AUDIT_API: str = Field(
            default="http://backend:8100",
            description="Audit Tool Server. Compose: http://backend:8100. Хост: http://localhost:8100",
        )
        TIMEOUT_SEC: int = Field(default=300)

    def __init__(self) -> None:
        self.valves = self.Valves()

    def _base(self) -> str:
        return self.valves.AUDIT_API.rstrip("/")

    async def create_case(
        self,
        inspection_name: str,
        keywords: str,
        period: str = "",
    ) -> str:
        """
        Создать кейс проверки. Вызови ПЕРВЫМ, когда аудитор назвал тему проверки.
        keywords — через запятую. period — год, если сказали.
        """
        kws = [x.strip() for x in keywords.split(",") if x.strip()]
        data = await self._req(
            "POST",
            "/api/v1/cases",
            {
                "inspection_name": inspection_name,
                "keywords": kws,
                "period": period or None,
            },
        )
        return (
            f"case_id={data.get('case_id')} status={data.get('status')}. "
            "Следующий шаг: propose_npa(case_id). Не качай документы."
        )

    async def propose_npa(self, case_id: str) -> str:
        """
        Предложить список НПА по кейсу. После ответа ПОКАЖИ список аудитору
        (номер, id, title, priority, why_needed) и ОСТАНОВИСЬ.
        Не вызывай select_npa и download_npa в том же ходе.
        """
        data = await self._req("POST", f"/api/v1/cases/{case_id}/propose")
        docs = data.get("documents") or []
        lines = [f"case_id={case_id} status={data.get('status')}"]
        for i, doc in enumerate(docs, start=1):
            lines.append(
                f"{i}. id={doc.get('id')} [{doc.get('priority')}] {doc.get('title')} "
                f"— {doc.get('why_needed')}"
            )
        lines.append(
            "Жди явного утверждения аудитора (номера или id). "
            "Без этого select_npa и download_npa запрещены."
        )
        return "\n".join(lines)

    async def select_npa(
        self,
        case_id: str,
        document_ids: str,
        manual_urls_json: str = "",
        extra_titles: str = "",
    ) -> str:
        """
        Утвердить акты. Вызывай ТОЛЬКО если аудитор в ЭТОМ сообщении назвал id, номера
        или дополнительные названия, которых нет в списке.
        document_ids — через запятую, это id из propose, не выдумывай.
        extra_titles — названия актов «от аудитора», через точку с запятой.
        manual_urls_json — опционально JSON object id→URL, если аудитор дал ссылку pravo.by.
        """
        ids = [x.strip() for x in document_ids.split(",") if x.strip()]
        body: dict = {"document_ids": ids}
        if extra_titles.strip():
            body["extra_titles"] = [
                x.strip() for x in extra_titles.split(";") if x.strip()
            ]
        if manual_urls_json.strip():
            import json

            body["manual_urls"] = json.loads(manual_urls_json)
        data = await self._req("POST", f"/api/v1/cases/{case_id}/select", body)
        return (
            f"status={data.get('status')} selected={data.get('selected_count')}. "
            "Теперь можно download_npa."
        )

    async def download_npa(self, case_id: str) -> str:
        """
        Скачать ТОЛЬКО уже утверждённые select_npa акты. Запрещено, если аудитор
        не утвердил список в этом чате. Не ищи ничего кроме allowlist сервера.
        """
        data = await self._req("POST", f"/api/v1/cases/{case_id}/download")
        fails = []
        for doc in data.get("documents") or []:
            if doc.get("selected") and doc.get("download_status") != "ok":
                fails.append(f"{doc.get('title')}: {doc.get('download_error')}")
        extra = "; ".join(fails) if fails else "все ок"
        return (
            f"status={data.get('status')} downloaded={data.get('downloaded')} "
            f"failed={data.get('failed')} ({extra}). Дальше sync_knowledge."
        )

    async def sync_knowledge(self, case_id: str) -> str:
        """Залить очищенные тексты кейса в Open WebUI Knowledge. После download_npa."""
        data = await self._req(
            "POST",
            f"/api/v1/cases/{case_id}/knowledge/openwebui/sync",
            {},
        )
        return str(data)[:1500]

    async def case_status(self, case_id: str) -> str:
        """Статус кейса, список актов, что скачано."""
        data = await self._req("GET", f"/api/v1/cases/{case_id}")
        docs = data.get("documents") or []
        lines = [
            f"case_id={data.get('case_id')} status={data.get('status')} "
            f"name={data.get('inspection_name')}"
        ]
        for doc in docs:
            lines.append(
                f"- {doc.get('id')} selected={doc.get('selected')} "
                f"{doc.get('download_status')} {doc.get('title')}"
            )
        return "\n".join(lines)

    async def ask_npa(self, case_id: str, question: str) -> str:
        """
        Вопрос к утверждённой библиотеке НПА кейса. Не используй для сумм и Excel.
        Если сервер говорит, что в фрагментах нет ответа — так и передай аудитору.
        """
        data = await self._req(
            "POST",
            f"/api/v1/cases/{case_id}/knowledge/ask",
            {"question": question},
        )
        sources = data.get("sources") or []
        cites = "; ".join(
            f"[{s.get('n')}] {s.get('title')}" for s in sources[:8]
        )
        return f"{data.get('answer')}\n\nsources: {cites}"

    async def list_cases(self) -> str:
        """Список проверок на этой машине."""
        data = await self._req("GET", "/api/v1/cases")
        if not data:
            return "кейсов нет"
        return "\n".join(
            f"{row.get('case_id')} {row.get('status')} {row.get('inspection_name')}"
            for row in data
        )

    async def build_brief(self, case_id: str, force: bool = False) -> str:
        """
        Собрать саммари библиотеки НПА (6–10 стр. Word) со ссылками на статьи.
        Вызывай, когда аудитор просит саммари / сводку / docx. После download_npa.
        """
        path = f"/api/v1/cases/{case_id}/knowledge/brief"
        if force:
            data = await self._req("POST", path, {"force": True})
        else:
            data = await self._req("POST", path, {})
        pages = data.get("pages_estimate")
        cites = data.get("citations")
        return (
            f"brief ready pages={pages} citations={cites}. "
            f"Скачать: {self._base()}{data.get('download') or path + '.docx'}"
        )

    async def build_program(self, case_id: str, force: bool = False) -> str:
        """
        Собрать программу аудиторской проверки банка РБ в Word.
        Вызывай, когда аудитор пишет «программа проверки». После download_npa.
        """
        path = f"/api/v1/cases/{case_id}/knowledge/program"
        if force:
            data = await self._req("POST", path, {"force": True})
        else:
            data = await self._req("POST", path, {})
        pages = data.get("pages_estimate")
        cites = data.get("citations")
        return (
            f"program ready pages={pages} citations={cites}. "
            f"Скачать: {self._base()}{data.get('download') or path + '.docx'}"
        )

    async def _req(self, method: str, path: str, json: Optional[dict] = None) -> dict:
        url = f"{self._base()}{path}"
        timeout = float(self.valves.TIMEOUT_SEC)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(method, url, json=json)
            if response.status_code >= 400:
                raise Exception(f"{response.status_code}: {response.text[:400]}")
            return response.json() if response.content else {}
