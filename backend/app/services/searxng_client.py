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


async def find_best_url(search_queries: list[str], title: str | None = None) -> dict | None:
    """Try queries in order; return first allowlisted hit.

    Expands site:pravo.gov.by → pravo.by / etalonline.by and always
    tries a plain title search as fallback.
    """
    expanded: list[str] = []
    for query in search_queries:
        q = (query or "").strip()
        if not q:
            continue
        expanded.append(q)
        if "pravo.gov.by" in q:
            expanded.append(q.replace("pravo.gov.by", "pravo.by"))
            expanded.append(q.replace("pravo.gov.by", "etalonline.by"))
        # also try without site: restriction
        if q.lower().startswith("site:"):
            parts = q.split(" ", 1)
            if len(parts) == 2 and parts[1].strip():
                expanded.append(parts[1].strip())

    if title:
        expanded.extend(
            [
                title,
                f"site:pravo.by {title}",
                f"site:etalonline.by {title}",
                f"site:nbrb.by {title}",
                f"site:minfin.gov.by {title}",
            ]
        )

    # de-dupe preserving order
    seen: set[str] = set()
    queries: list[str] = []
    for q in expanded:
        if q not in seen:
            seen.add(q)
            queries.append(q)

    for query in queries:
        hits = await search_searxng(query)
        if hits:
            return hits[0]
    return None
