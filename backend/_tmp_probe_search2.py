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
    "Referer": "https://pravo.by/",
}


def links(html: str, base: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    out = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"])
        title = " ".join((a.get_text() or "").split())[:110]
        low = href.lower()
        if any(x in low for x in ("guid=", "p0=", "regnum=", "/document/", "webnpa")):
            out.append((href, title))
    return out


def main() -> None:
    q = "внутренний контроль в банках"
    with httpx.Client(timeout=30, follow_redirects=True, headers=headers) as client:
        for where in ("portal", "etalon", "nacreestr", "tbd"):
            r = client.get("https://pravo.by/search/", params={"search": q, "where": where})
            soup = BeautifulSoup(r.text, "lxml")
            iframes = [i.get("src") for i in soup.find_all("iframe")]
            print(f"\nwhere={where} {r.url} len={len(r.content)} iframes={iframes[:4]}")
            hits = links(r.text, str(r.url))
            print(" hits", len(hits))
            for href, title in hits[:10]:
                print(" ", href[:150], "|", title)
            # look for 'найдено' or result classes
            text = " ".join(soup.get_text(" ", strip=True).split())
            for needle in ("Найдено", "результатов", "ничего не", "ЭТАЛОН", "реестр"):
                if needle.lower() in text.lower():
                    i = text.lower().find(needle.lower())
                    print(" ", needle, "->", text[max(0, i - 40) : i + 120])

        # POST nbrb legislation search
        page = client.get("https://www.nbrb.by/legislation/search")
        soup = BeautifulSoup(page.text, "lxml")
        token = ""
        tok = soup.find("input", {"name": "__RequestVerificationToken"})
        if tok:
            token = tok.get("value") or ""
        r = client.post(
            "https://www.nbrb.by/legislation/search",
            data={
                "optype": "",
                "Name": "внутренний контроль",
                "Num": "",
                "Text": "",
                "NRLANum": "",
                "__RequestVerificationToken": token,
            },
            headers={**headers, "Origin": "https://www.nbrb.by", "Referer": "https://www.nbrb.by/legislation/search"},
        )
        print("\nNBRB POST", r.status_code, r.url, "len", len(r.content))
        hits = links(r.text, str(r.url))
        print(" hits", len(hits))
        for href, title in hits[:15]:
            print(" ", href[:150], "|", title)
        text = " ".join(BeautifulSoup(r.text, "lxml").get_text(" ", strip=True).split())
        print(" text snippet:", text[text.find("Поиск") : text.find("Поиск") + 500] if "Поиск" in text else text[:400])


if __name__ == "__main__":
    main()
