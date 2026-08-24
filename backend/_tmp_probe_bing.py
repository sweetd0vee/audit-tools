from __future__ import annotations

from html import unescape
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

headers = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


def unwrap(url: str) -> str:
    url = unescape(url)
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    for key in ("uddg", "u", "url", "q", "u2"):
        if qs.get(key):
            return unquote(qs[key][0])
    return url


def main() -> None:
    q = 'site:pravo.by "внутренний аудит" банки инструкция'
    with httpx.Client(timeout=25, follow_redirects=True, headers=headers) as client:
        r = client.get("https://www.bing.com/search", params={"q": q, "count": 20})
        print("bing", r.status_code, len(r.text))
        soup = BeautifulSoup(r.text, "lxml")
        n = 0
        for a in soup.select("li.b_algo h2 a, h2 a, a[href]"):
            href = unwrap(a.get("href") or "")
            title = " ".join((a.get_text() or "").split())[:100]
            if "pravo" in href.lower() or "etalonline" in href.lower() or "nbrb" in href.lower() or "pravo" in title.lower():
                print(" A", href[:160], "|", title)
                n += 1
            if n >= 15:
                break
        cites = [c.get_text(" ", strip=True) for c in soup.select("cite")]
        print("cites", cites[:12])
        # save a slice
        Path = __import__("pathlib").Path
        Path("_tmp_bing.html").write_text(r.text[:80000], encoding="utf-8")
        print("saved _tmp_bing.html")

        r2 = client.get(
            "https://pravo.by/search/index.php",
            params={"q": "внутренний контроль банки", "s": "", "how": "r"},
        )
        Path("_tmp_pravo_search.html").write_text(r2.text, encoding="utf-8")
        print("pravo search saved", r2.status_code, len(r2.text))
        soup2 = BeautifulSoup(r2.text, "lxml")
        for sel in (".search-item", ".search-page", ".search-preview", ".search-result", "#search-result", ".search-tags"):
            print(sel, len(soup2.select(sel)))
        print("classes sample:", sorted({tuple(c.get("class") or []) for c in soup2.find_all(True) if c.get("class")})[:40])


if __name__ == "__main__":
    main()
