from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.config import settings
from app.prompts import prompt
from app.services.extra_titles import search_queries_for_title
from app.services.known_sources import (
    catalog_act_by_number,
    catalog_entries,
    catalog_prompt_block,
    match_catalog_act,
)

logger = logging.getLogger(__name__)
_CLIENTS: dict[float, httpx.AsyncClient] = {}


def _ollama_client(timeout: float | None) -> httpx.AsyncClient:
    value = float(timeout or settings.ollama_timeout_sec)
    client = _CLIENTS.get(value)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=value)
        _CLIENTS[value] = client
    return client


async def close_clients() -> None:
    clients = list(_CLIENTS.values())
    _CLIENTS.clear()
    for client in clients:
        await client.aclose()


def _extract_json(text: str) -> dict[str, Any]:
    parsed = extract_json_value(text)
    if not isinstance(parsed, dict):
        raise ValueError("LLM did not return a JSON object")
    return parsed


def extract_json_value(text: str) -> Any:
    """Parse JSON object or array from model output (raw or fenced)."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for pattern in (r"\{[\s\S]*\}", r"\[[\s\S]*\]"):
        match = re.search(pattern, text)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                continue
    raise ValueError("LLM did not return JSON")


def build_user_prompt(
    inspection_name: str,
    keywords: list[str],
    max_docs: int | None = None,
) -> str:
    n_catalog = len(catalog_entries())
    max_docs = min(max_docs or settings.max_docs_to_propose, n_catalog)
    min_docs = min(4, max_docs)
    keywords_str = ", ".join(keywords) if keywords else "(не указаны)"
    return prompt(
        "propose_user",
        inspection_name=inspection_name,
        keywords_str=keywords_str,
        catalog=catalog_prompt_block(),
        min_docs=min_docs,
        max_docs=max_docs,
    )


def _priority(value: object) -> int:
    try:
        priority = int(value or 2)
    except (TypeError, ValueError):
        priority = 2
    return min(3, max(1, priority))


def _search_queries(raw: object, title: str) -> list[str]:
    queries = raw if isinstance(raw, list) else [raw] if raw else []
    queries = [str(q).strip() for q in queries if str(q).strip()]
    return queries or search_queries_for_title(title) or [f"site:pravo.gov.by {title}"]


def bind_documents_to_catalog(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only catalogued acts and attach their official URLs."""
    bound: dict[str, dict[str, Any]] = {}
    for doc in documents:
        act = catalog_act_by_number(doc.get("n") or doc.get("catalog_n"))
        if act is None:
            act = match_catalog_act(str(doc.get("title") or ""))
        if act is None:
            continue
        why = str(doc.get("why_needed") or "").strip()
        priority = _priority(doc.get("priority"))
        current = bound.get(act.url)
        if current is not None:
            if priority < current["priority"]:
                current["priority"] = priority
                if why:
                    current["why_needed"] = why
            elif why and not current["why_needed"]:
                current["why_needed"] = why
            continue
        bound[act.url] = {
            "title": act.title,
            "doc_type": act.doc_type,
            "why_needed": why,
            "search_queries": _search_queries(doc.get("search_queries"), act.title),
            "priority": priority,
            "found_url": act.url,
        }
    return list(bound.values())


def normalize_documents(parsed: dict[str, Any], max_docs: int) -> tuple[list[str], list[dict[str, Any]]]:
    topics = parsed.get("topics") or []
    documents = parsed.get("documents") or []
    if not isinstance(documents, list) or not documents:
        raise ValueError("LLM returned empty documents list")

    clean_docs: list[dict[str, Any]] = []
    for doc in documents[: max(max_docs * 2, max_docs)]:
        if not isinstance(doc, dict):
            continue
        title = str(doc.get("title") or "").strip()
        n = doc.get("n") if doc.get("n") is not None else doc.get("catalog_n")
        if not title and n is None:
            continue
        clean_docs.append(
            {
                "n": n,
                "title": title,
                "why_needed": str(doc.get("why_needed") or "").strip(),
                "search_queries": doc.get("search_queries") or [],
                "priority": _priority(doc.get("priority")),
            }
        )

    bound = bind_documents_to_catalog(clean_docs)[:max_docs]
    if not bound:
        raise ValueError(
            "LLM did not pick any act from the official catalog; "
            "proposed titles must match known_sources"
        )

    return [str(t).strip() for t in topics if str(t).strip()], bound


async def propose_documents_events(
    inspection_name: str,
    keywords: list[str],
    max_docs: int | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield debug/chat/token events, then a final 'result' event."""
    max_docs = max_docs or settings.max_docs_to_propose
    t0 = time.perf_counter()

    def elapsed_ms() -> int:
        return int((time.perf_counter() - t0) * 1000)

    user_prompt = build_user_prompt(inspection_name, keywords, max_docs)

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
    max_docs: int | None = None,
) -> dict[str, Any]:
    """Non-stream wrapper used by classic /propose."""
    result: dict[str, Any] | None = None
    async for event in propose_documents_events(
        inspection_name, keywords, max_docs
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
        vectors: list[list[float]] = []
        for text in texts:
            legacy = await client.post(
                f"{settings.ollama_base_url}/api/embeddings",
                json={"model": settings.ollama_embed_model, "prompt": text},
            )
            legacy.raise_for_status()
            one = legacy.json().get("embedding") or []
            vectors.append(list(map(float, one)))
        return vectors
    resp.raise_for_status()
    data = resp.json()
    vectors = data.get("embeddings") or []
    if not vectors and data.get("embedding"):
        vectors = [data["embedding"]]
    return [list(map(float, v)) for v in vectors]


# Qwen3-Reranker is a causal LM: P(yes|query, doc). Ollama has no /api/rerank
# (0.32 returns 404), so we score via /api/generate + yes/no token.
_RERANK_INSTRUCT = (
    "Given a question about Belarusian statutes and regulations, retrieve the "
    "passage that contains the applicable article, clause, or rule."
)
_RERANK_DOC_CHARS = 4000
_RERANK_CONCURRENCY = 4
_rerank_unavailable = False


def format_rerank_prompt(query: str, document: str, instruct: str = _RERANK_INSTRUCT) -> str:
    doc = " ".join((document or "").split())
    if len(doc) > _RERANK_DOC_CHARS:
        doc = doc[:_RERANK_DOC_CHARS]
    q = " ".join((query or "").split())
    return (
        "<|im_start|>system\n"
        "Judge whether the Document meets the requirements based on the Query "
        'and the Instruct provided. Note that the answer can only be "yes" or "no".'
        "<|im_end|>\n"
        "<|im_start|>user\n"
        f"<Instruct>: {instruct}\n"
        f"<Query>: {q}\n"
        f"<Document>: {doc}<|im_end|>\n"
        "<|im_start|>assistant\n"
        "<think>\n\n</think>\n\n"
    )


def _token_logprob(entry: Any, name: str) -> float | None:
    if not isinstance(entry, dict):
        return None
    key = name.lower()
    if str(entry.get("token") or "").strip().lower() == key:
        lp = entry.get("logprob")
        try:
            return float(lp) if lp is not None else None
        except (TypeError, ValueError):
            return None
    for item in entry.get("top_logprobs") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("token") or "").strip().lower() != key:
            continue
        lp = item.get("logprob")
        try:
            return float(lp) if lp is not None else None
        except (TypeError, ValueError):
            continue
    return None


def score_rerank_response(text: str, data: dict[str, Any] | None = None) -> float:
    """Map a generate() payload to P(yes). Falls back to the decoded token."""
    payload = data or {}
    logprobs = payload.get("logprobs")
    if isinstance(logprobs, dict):
        content = logprobs.get("content") or logprobs.get("tokens") or []
        first = content[0] if content else logprobs
        yes_lp = _token_logprob(first, "yes")
        no_lp = _token_logprob(first, "no")
        if yes_lp is not None or no_lp is not None:
            yes_s = math.exp(yes_lp) if yes_lp is not None else 0.0
            no_s = math.exp(no_lp) if no_lp is not None else 0.0
            denom = yes_s + no_s
            if denom > 0:
                return yes_s / denom
    token = (text or "").strip().lower()
    if token.startswith("yes") or token.startswith("да"):
        return 1.0
    if token.startswith("no") or token.startswith("нет"):
        return 0.0
    return 0.5


async def _rerank_one(query: str, document: str) -> float:
    payload = {
        "model": settings.ollama_rerank_model,
        "prompt": format_rerank_prompt(query, document),
        "stream": False,
        "raw": True,
        "think": False,
        "options": {
            "temperature": 0.0,
            "num_predict": 1,
            "num_ctx": 4096,
        },
    }
    client = _ollama_client(settings.rerank_timeout_sec)
    resp = await client.post(f"{settings.ollama_base_url}/api/generate", json=payload)
    if resp.status_code == 404:
        raise FileNotFoundError(settings.ollama_rerank_model)
    resp.raise_for_status()
    data = resp.json()
    return score_rerank_response(str(data.get("response") or ""), data)


async def rerank_texts(query: str, documents: list[str]) -> list[float]:
    """Score (query, doc) pairs. Empty list = caller keeps hybrid order."""
    global _rerank_unavailable
    model = (settings.ollama_rerank_model or "").strip()
    if _rerank_unavailable or not model or not documents:
        return []
    sem = asyncio.Semaphore(_RERANK_CONCURRENCY)

    async def _one(doc: str) -> float:
        async with sem:
            return await _rerank_one(query, doc)

    try:
        return list(await asyncio.gather(*[_one(doc) for doc in documents]))
    except FileNotFoundError:
        _rerank_unavailable = True
        logger.warning("rerank model missing, disabling: %s", model)
        return []
    except Exception:
        logger.exception("rerank failed query_len=%s docs=%s", len(query), len(documents))
        return []
