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
    print(f"\n=== {label} {url} len={len(html)}")
    n = 0
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"])
        title = " ".join((a.get_text() or "").split())[:120]
        low = href.lower()
        if any(x in low for x in ("guid=", "p0=", "regnum=", "/document/", "webnpa", "main.aspx")):
            print(f"  {href[:170]} | {title}")
            n += 1
            if n >= 15:
                break
    if n == 0:
        text = " ".join(soup.get_text(" ", strip=True).split())
        print(" TEXT", text[:600])


def main() -> None:
    q = "внутренний аудит в банках"
    with httpx.Client(timeout=30, follow_redirects=True, headers=headers) as client:
        urls = [
            ("aspx", f"https://pravo.by/main.aspx?guid=1051&querytext={q}"),
            ("reestr p0", f"https://pravo.by/natsionalnyy-reestr/poisk-v-reestre/?p0={q}"),
            ("www etal", f"https://www.etalonline.by/search/?search_str={q}"),
            ("etal http", f"http://www.etalonline.by/search/?search_str={q}"),
        ]
        for label, url in urls:
            try:
                r = client.get(url)
                dump(label, str(r.url), r.text)
            except Exception as exc:
                print(label, "ERR", exc)


if __name__ == "__main__":
    main()
