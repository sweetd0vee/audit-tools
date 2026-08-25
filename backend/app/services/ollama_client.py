from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.config import settings
from app.prompts import prompt

_CLIENTS: dict[float, httpx.AsyncClient] = {}


def _ollama_client(timeout: float | None) -> httpx.AsyncClient:
    value = float(timeout or settings.ollama_timeout_sec)
    client = _CLIENTS.get(value)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=value)
        _CLIENTS[value] = client
    return client


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        raise ValueError("LLM did not return JSON")
    return json.loads(match.group(0))


def build_user_prompt(
    inspection_name: str,
    keywords: list[str],
    period: str | None = None,
    max_docs: int | None = None,
) -> str:
    max_docs = max_docs or settings.max_docs_to_propose
    keywords_str = ", ".join(keywords) if keywords else "(не указаны)"
    period_str = period or "не указан"
    return prompt(
        "propose_user",
        inspection_name=inspection_name,
        keywords_str=keywords_str,
        period_str=period_str,
        min_docs=max(8, max_docs - 3),
        max_docs=max_docs,
    )


def normalize_documents(parsed: dict[str, Any], max_docs: int) -> tuple[list[str], list[dict[str, Any]]]:
    topics = parsed.get("topics") or []
    documents = parsed.get("documents") or []
    if not isinstance(documents, list) or not documents:
        raise ValueError("LLM returned empty documents list")

    clean_docs: list[dict[str, Any]] = []
    for doc in documents[:max_docs]:
        if not isinstance(doc, dict):
            continue
        title = str(doc.get("title") or "").strip()
        if not title:
            continue
        queries = doc.get("search_queries") or []
        if isinstance(queries, str):
            queries = [queries]
        queries = [str(q).strip() for q in queries if str(q).strip()]
        if not queries:
            queries = [f"site:pravo.gov.by {title}"]
        priority = int(doc.get("priority") or 2)
        priority = min(3, max(1, priority))
        clean_docs.append(
            {
                "title": title,
                "doc_type": str(doc.get("doc_type") or "иное").strip().lower(),
                "why_needed": str(doc.get("why_needed") or "").strip(),
                "search_queries": queries,
                "priority": priority,
            }
        )

    if not clean_docs:
        raise ValueError("No valid documents after normalization")

    return [str(t).strip() for t in topics if str(t).strip()], clean_docs


async def propose_documents_events(
    inspection_name: str,
    keywords: list[str],
    period: str | None = None,
    max_docs: int | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield debug/chat/token events, then a final 'result' event."""
    max_docs = max_docs or settings.max_docs_to_propose
    t0 = time.perf_counter()

    def elapsed_ms() -> int:
        return int((time.perf_counter() - t0) * 1000)

    user_prompt = build_user_prompt(inspection_name, keywords, period, max_docs)

    yield {
        "type": "status",
        "message": f"Подготовка запроса к Ollama ({settings.ollama_model})",
        "elapsed_ms": elapsed_ms(),
    }
    yield {
        "type": "chat",
        "role": "system",
        "content": prompt("propose_system"),
        "elapsed_ms": elapsed_ms(),
    }
    yield {
        "type": "chat",
        "role": "user",
        "content": user_prompt,
        "elapsed_ms": elapsed_ms(),
    }
    yield {
        "type": "status",
        "message": "Ожидание ответа модели (stream)…",
        "elapsed_ms": elapsed_ms(),
    }

    payload = {
        "model": settings.ollama_model,
        "stream": True,
        "format": "json",
        "think": False,
        "options": {
            "temperature": 0.2,
            "num_ctx": settings.ollama_num_ctx,
        },
        "messages": [
            {"role": "system", "content": prompt("propose_system")},
            {"role": "user", "content": user_prompt},
        ],
    }

    content_parts: list[str] = []
    client = _ollama_client(settings.ollama_timeout_sec)
    async with client.stream(
        "POST",
        f"{settings.ollama_base_url}/api/chat",
        json=payload,
    ) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue
            piece = (chunk.get("message") or {}).get("content") or ""
            if piece:
                content_parts.append(piece)
                yield {
                    "type": "token",
                    "content": piece,
                    "elapsed_ms": elapsed_ms(),
                }
            if chunk.get("done"):
                break

    content = "".join(content_parts)
    yield {
        "type": "chat",
        "role": "assistant",
        "content": content,
        "elapsed_ms": elapsed_ms(),
    }
    yield {
        "type": "status",
        "message": "Парсинг JSON ответа модели…",
        "elapsed_ms": elapsed_ms(),
    }

    parsed = _extract_json(content)
    topics, clean_docs = normalize_documents(parsed, max_docs)
    total_ms = elapsed_ms()

    yield {
        "type": "result",
        "elapsed_ms": total_ms,
        "payload": {
            "topics": topics,
            "documents": clean_docs,
            "model": settings.ollama_model,
            "raw": content,
            "system_prompt": prompt("propose_system"),
            "user_prompt": user_prompt,
            "elapsed_ms": total_ms,
        },
    }


async def propose_documents(
    inspection_name: str,
    keywords: list[str],
    period: str | None = None,
    max_docs: int | None = None,
) -> dict[str, Any]:
    """Non-stream wrapper used by classic /propose."""
    result: dict[str, Any] | None = None
    async for event in propose_documents_events(
        inspection_name, keywords, period, max_docs
    ):
        if event.get("type") == "result":
            result = event["payload"]
    if not result:
        raise ValueError("Propose stream finished without result")
    return result


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _strip_think(text: str) -> str:
    cleaned = _THINK_RE.sub("", text or "")
    return cleaned.strip()


async def chat_complete(
    system: str,
    user: str,
    *,
    temperature: float = 0.2,
    timeout: float | None = None,
    num_ctx: int | None = None,
    num_predict: int | None = None,
) -> str:
    """Single-shot chat completion (no stream)."""
    return await chat_messages(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        timeout=timeout,
        num_ctx=num_ctx,
        num_predict=num_predict,
    )


async def chat_messages(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.2,
    timeout: float | None = None,
    num_ctx: int | None = None,
    num_predict: int | None = None,
) -> str:
    """Multi-turn chat completion (no stream)."""
    options: dict[str, Any] = {"temperature": temperature}
    ctx = num_ctx if num_ctx is not None else settings.ollama_num_ctx
    if ctx:
        options["num_ctx"] = int(ctx)
    if num_predict:
        options["num_predict"] = int(num_predict)
    payload = {
        "model": settings.ollama_model,
        "stream": False,
        "think": False,
        "options": options,
        "messages": messages,
    }
    client = _ollama_client(timeout or settings.ollama_timeout_sec)
    resp = await client.post(f"{settings.ollama_base_url}/api/chat", json=payload)
    resp.raise_for_status()
    data = resp.json()
    message = data.get("message") or {}
    content = str(message.get("content") or "").strip()
    return _strip_think(content)


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed texts via Ollama. Empty strings become zero-vectors after first success."""
    if not texts:
        return []
    payload = {
        "model": settings.ollama_embed_model,
        "input": texts,
    }
    client = _ollama_client(settings.embed_timeout_sec)
    resp = await client.post(f"{settings.ollama_base_url}/api/embed", json=payload)
    if resp.status_code == 404:
        resp = await client.post(
            f"{settings.ollama_base_url}/api/embeddings",
            json={"model": settings.ollama_embed_model, "prompt": texts[0]},
        )
        resp.raise_for_status()
        one = resp.json().get("embedding") or []
        return [one]
    resp.raise_for_status()
    data = resp.json()
    vectors = data.get("embeddings") or []
    if not vectors and data.get("embedding"):
        vectors = [data["embedding"]]
    return [list(map(float, v)) for v in vectors]
