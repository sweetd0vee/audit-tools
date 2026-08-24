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


def dump_selects(html: str) -> None:
    soup = BeautifulSoup(html, "lxml")
    el = soup.find(id="searchwhere") or soup.find(attrs={"name": "where"})
    print("where el", el)
    if el:
        for opt in el.find_all("option"):
            print(" option", opt.get("value"), "|", opt.get_text(strip=True)[:80])


def dump_hits(label: str, url: str, html: str, n: int = 15) -> None:
    soup = BeautifulSoup(html, "lxml")
    print(f"\n=== {label} {url} len={len(html)}")
    count = 0
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"])
        title = " ".join((a.get_text() or "").split())[:120]
        low = href.lower()
        useful = any(
            x in low
            for x in (
                "/document/",
                "guid=",
                "regnum=",
                "webnpa",
                "/legislation/",
                ".pdf",
                "/upload/",
                "search",
            )
        )
        if not useful or not title or len(title) < 8:
            continue
        print(f"  {href[:150]} | {title}")
        count += 1
        if count >= n:
            break


def main() -> None:
    with httpx.Client(timeout=30, follow_redirects=True, headers=headers) as client:
        home = client.get("https://pravo.by/")
        dump_selects(home.text)

        q = "внутренний аудит банках"
        for where in ("", "iblock_news", "etalon", "reestr", "documents", "1", "2"):
            r = client.get("https://pravo.by/search/", params={"q": q, "where": where} if where else {"q": q})
            soup = BeautifulSoup(r.text, "lxml")
            items = soup.select(".search-item, .search-page, .search-result, .search-preview, li")
            print(f"where={where!r} status={r.status_code} items_guess={len(items)} url={r.url}")
            text = " ".join(soup.get_text(" ", strip=True).split())
            idx = text.find("Поиск")
            print(" snippet:", text[idx : idx + 400] if idx >= 0 else text[:300])

        r = client.get("https://www.nbrb.by/legislation/search")
        dump_hits("nbrb legislation/search form", str(r.url), r.text, 8)
        soup = BeautifulSoup(r.text, "lxml")
        for form in soup.find_all("form")[:5]:
            print("FORM", form.get("action"), form.get("method"))
            for inp in form.find_all(["input", "select", "textarea"])[:25]:
                name = inp.get("name")
                if name:
                    print(" ", name, inp.get("type"), (inp.get("value") or "")[:60])

        r = client.get(
            "https://www.nbrb.by/legislation/search",
            params={"search": "внутренний аудит", "q": "внутренний аудит"},
        )
        dump_hits("nbrb legislation q", str(r.url), r.text)


if __name__ == "__main__":
    main()
