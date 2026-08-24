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


def dump(label: str, url: str, html: str) -> None:
    soup = BeautifulSoup(html, "lxml")
    print(f"\n=== {label} len={len(html)}")
    n = 0
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"])
        title = " ".join((a.get_text() or "").split())[:130]
        if "regnum=" not in href.lower() or not title:
            continue
        # skip codes sidebar if title is just "X кодекс"
        print(f"  {href[:140]} | {title}")
        n += 1
        if n >= 12:
            break
    text = " ".join(soup.get_text(" ", strip=True).split())
    for needle in ("Найдено документов", "Данный функционал", "Оплатите"):
        i = text.find(needle)
        if i >= 0:
            print(" ", needle, "->", text[i : i + 80])


def main() -> None:
    q = "внутренний аудит в банках"
    base = "https://etalonline.by/search/"
    common = {
        "adv_s": "0",
        "s": "1",
        "d": "1",
        "ps": "10",
        "force": "1",
        "db[]": ["0", "1", "2"],
        "organ_m": "or",
        "keyw_m": "or",
    }
    with httpx.Client(timeout=40, follow_redirects=True, headers=headers) as client:
        trials = [
            ("search_str s=1", {**common, "search_str": q}),
            ("akt_name s=1", {**common, "akt_name": q, "search_str": ""}),
            ("keyword s=1", {**common, "keyword": q, "search_str": ""}),
            ("search_str only force", {"search_str": q, "s": "1", "force": "1", "d": "1"}),
        ]
        for label, params in trials:
            r = client.get(base, params=params)
            dump(label, str(r.url), r.text)


if __name__ == "__main__":
    main()
