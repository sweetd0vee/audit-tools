from __future__ import annotations

NEWS_MARKERS = ("/novosti/", "/analitika/", "/news/")

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

DOWNLOAD_BROWSER_HEADERS = {
    **BROWSER_HEADERS,
    "Accept": "application/pdf,text/html,application/xhtml+xml,*/*;q=0.8",
}
