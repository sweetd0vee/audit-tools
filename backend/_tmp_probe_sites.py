from __future__ import annotations

import re

import httpx
from bs4 import BeautifulSoup

headers = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

HREF_RE = re.compile(r"""https?://[^\s"'<>\\]+""", re.I)
DOC_RE = re.compile(r"(guid=\d+|p0=|regnum=|/document/|webnpa/text)", re.I)


def show_links(label: str, text: str, limit: int = 10) -> None:
    seen: set[str] = set()
    found: list[str] = []
    for raw in HREF_RE.findall(text or ""):
        url = raw.rstrip(").,;]")
        if DOC_RE.search(url) and url not in seen:
            seen.add(url)
            found.append(url)
    print(f"=== {label} doc_urls={len(found)}")
    for url in found[:limit]:
        print(" ", url[:180])


def main() -> None:
    q = "внутренний аудит в банках"
    with httpx.Client(timeout=25, follow_redirects=True, headers=headers) as client:
        try:
            r = client.get("http://localhost:8080/search", params={"q": q, "format": "json"})
            print("=== searx", r.status_code, r.text[:400])
        except Exception as exc:
            print("=== searx ERR", exc)

        trials = [
            ("pravo search q", "https://pravo.by/search/", {"q": q}),
            ("pravo search search", "https://pravo.by/search/", {"search": q}),
            ("pravo index q", "https://pravo.by/search/index.php", {"q": q}),
            ("pravo root search", "https://pravo.by/", {"search": q}),
            ("pravo document p0", "https://pravo.by/document/", {"guid": "3871", "p0": q}),
            ("nbrb search", "https://www.nbrb.by/search", {"search": q}),
            ("nbrb q", "https://www.nbrb.by/search", {"q": q}),
            ("ddg lite", "https://lite.duckduckgo.com/lite/", {"q": f"site:pravo.by {q}"}),
            ("ya.ru", "https://ya.ru/search/", {"text": f"site:pravo.by {q}"}),
        ]
        for label, url, params in trials:
            try:
                r = client.get(url, params=params)
                print(f"=== {label} {r.status_code} {r.url} len={len(r.content)}")
                show_links(label, r.text, 6)
            except Exception as exc:
                print(f"=== {label} ERR {exc}")

        # etalonline POST
        try:
            r = client.post(
                "https://etalonline.by/search/",
                data={"search_str": q, "s": "1", "adv_s": "0"},
            )
            print("=== etal POST", r.status_code, "len", len(r.content), r.url)
            show_links("etal POST", r.text, 8)
            soup = BeautifulSoup(r.text, "lxml")
            titles = [a.get_text(" ", strip=True)[:80] for a in soup.find_all("a", href=True) if "regnum=" in (a.get("href") or "")]
            print(" titles", titles[:8])
        except Exception as exc:
            print("=== etal POST ERR", exc)


if __name__ == "__main__":
    main()
