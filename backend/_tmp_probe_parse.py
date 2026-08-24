from __future__ import annotations

from urllib.parse import urljoin

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

QUERIES = [
    "внутренний аудит в банках",
    "о порядке проведения внутреннего аудита в банках",
    "оформления и хранения банковских документов",
    "внутреннем контроле при осуществлении банковских операций",
    "об аренде и безвозмездном пользовании имуществом",
    "Инструкция НБРБ № 38",
    "Положение о бухгалтерском учете аренды",
]


def dump_hits(label: str, url: str, html: str) -> None:
    soup = BeautifulSoup(html, "lxml")
    print(f"\n=== {label} {url} len={len(html)}")
    n = 0
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"])
        low = href.lower()
        if not any(x in low for x in ("/document/", "guid=", "regnum=", "webnpa", "legislation", ".pdf")):
            continue
        title = " ".join((a.get_text() or "").split())[:110]
        if not title:
            continue
        print(f"  {href[:140]} | {title}")
        n += 1
        if n >= 12:
            break
    if n == 0:
        # print snippets of result-looking blocks
        text = " ".join(soup.get_text(" ", strip=True).split())
        print("  TEXT:", text[:500])


def main() -> None:
    with httpx.Client(timeout=30, follow_redirects=True, headers=headers) as client:
        for q in QUERIES[:4]:
            r = client.get("https://pravo.by/search/", params={"q": q})
            dump_hits("pravo", str(r.url), r.text)
            r2 = client.get("https://www.nbrb.by/search", params={"search": q})
            dump_hits("nbrb", str(r2.url), r2.text)


if __name__ == "__main__":
    main()
