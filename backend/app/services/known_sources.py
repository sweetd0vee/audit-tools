"""Curated official NPA entry points (Belarus).

Used when SearXNG engines are rate-limited / suspended.
Keys are lowercase substrings matched against proposed document titles.
"""

from __future__ import annotations

# title substring (lowercase) -> preferred official URL
KNOWN_NPA_URLS: list[tuple[str, str]] = [
    (
        "гражданский кодекс",
        "https://pravo.by/document/?guid=3871&p0=hk9800218",
    ),
    (
        "налоговый кодекс",
        "https://pravo.by/document/?guid=3871&p0=Hk0200166",
    ),
    (
        "банковский кодекс",
        "https://pravo.by/document/?guid=3871&p0=Hk0000441",
    ),
    (
        "о валютном регулировании",
        "https://pravo.by/document/?guid=12551&p0=H12200136&p1=1",
    ),
    (
        "валютном регулировании и валютном контроле",
        "https://www.nbrb.by/legislation/forexregul",
    ),
    (
        "валютн",
        "https://www.nbrb.by/legislation/forexregul",
    ),
    (
        "об аудиторской деятельности",
        "https://pravo.by/document/?guid=12551&p0=H11300056&p1=1",
    ),
    (
        "национальн",
        "https://www.minfin.gov.by/ru/auditor_activities/normative/",
    ),
    (
        "аренда",
        "https://pravo.by/document/?guid=3871&p0=hk9800218",
    ),
]


def lookup_known_url(title: str) -> str | None:
    t = (title or "").lower()
    for needle, url in KNOWN_NPA_URLS:
        if needle in t:
            return url
    return None
