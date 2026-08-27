from __future__ import annotations

import httpx

from app.config import settings
from app.domains import host_allowed


async def search_searxng(query: str, count: int | None = None) -> list[dict]:
    """Search via self-hosted SearXNG, keep only allowlisted domains."""
    count = count or settings.max_search_results_per_query
    params = {
        "q": query,
        "format": "json",
    }

    headers = {
        # SearXNG botdetection requires these when calling locally
        "X-Forwarded-For": "127.0.0.1",
        "X-Real-IP": "127.0.0.1",
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(timeout=settings.searxng_timeout_sec, headers=headers) as client:
        resp = await client.get(settings.searxng_url, params=params)
        resp.raise_for_status()
        data = resp.json()

    results = []
    for item in data.get("results") or []:
        url = item.get("url") or item.get("href") or ""
        if not url or not host_allowed(url):
            continue
        results.append(
            {
                "title": item.get("title") or "",
                "url": url,
                "snippet": item.get("content") or item.get("snippet") or "",
                "engine": item.get("engine") or "",
            }
        )
        if len(results) >= count:
            break
    return results
