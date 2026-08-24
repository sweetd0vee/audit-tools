from __future__ import annotations

import re

import httpx

from app.services.downloader import usable_url

headers = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

q = "site:pravo.by внутренний аудит в банках инструкция"
endpoints = [
    ("ddg", "https://html.duckduckgo.com/html/", {"q": q}, True),
    ("bing", "https://www.bing.com/search", {"q": q}, False),
    ("yandex", "https://yandex.ru/search/", {"text": q, "lr": "149"}, False),
    ("searx", "http://localhost:8080/search", {"q": q, "format": "json"}, False),
]

href_re = re.compile(r"""https?://[^\s"'<>\\]+""", re.I)

with httpx.Client(timeout=20, follow_redirects=True, headers=headers) as client:
    for name, url, params, post in endpoints:
        try:
            resp = client.post(url, data=params) if post else client.get(url, params=params)
            print("===", name, resp.status_code, "len", len(resp.content))
            urls = []
            seen: set[str] = set()
            for raw in href_re.findall(resp.text):
                item = raw.rstrip(").,;]")
                if "pravo.by" in item or "etalonline.by" in item or "nbrb.by" in item:
                    cleaned = usable_url(item) or item
                    if cleaned not in seen:
                        seen.add(cleaned)
                        urls.append(cleaned[:160])
            print(" official urls", len(urls))
            for item in urls[:8]:
                print("  ", item)
        except Exception as exc:  # noqa: BLE001
            print("===", name, "ERR", type(exc).__name__, exc)
