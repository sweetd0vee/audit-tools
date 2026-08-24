from __future__ import annotations

import re
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
    print(f"\n=== {label} {url[:120]} len={len(html)}")
    n = 0
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"])
        title = " ".join((a.get_text() or "").split())[:120]
        low = href.lower()
        if any(x in low for x in ("guid=", "/document/", "regnum=", "webnpa", "p0=")) and title:
            print(f"  {href[:160]} | {title}")
            n += 1
            if n >= 12:
                break
    if n == 0:
        text = " ".join(soup.get_text(" ", strip=True).split())
        print(" TEXT", text[:500])


def main() -> None:
    q = "внутренний контроль в банках"
    with httpx.Client(timeout=30, follow_redirects=True, headers=headers) as client:
        r = client.get(
            "https://pravo.by/pravovaya-informatsiya/pravovye-akty-po-temam/poisk-v-tbd/",
            params={"p0": q},
        )
        dump("tbd", str(r.url), r.text)

        r = client.get("https://etalonline.by/search/")
        # find ajax/api
        urls = sorted(set(re.findall(r"[\"'](/[^\"']*(?:search|ajax|api)[^\"']*)[\"']", r.text, re.I)))
        print("\netal script urls", urls[:30])
        srcs = [s.get("src") for s in BeautifulSoup(r.text, "lxml").find_all("script") if s.get("src")]
        print("scripts", srcs[:20])


if __name__ == "__main__":
    main()
