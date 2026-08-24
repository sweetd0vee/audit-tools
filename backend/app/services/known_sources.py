"""Curated official NPA entry points (Belarus).

Used when SearXNG engines are rate-limited / suspended.
Keys are lowercase substrings matched against proposed document titles.
Longer / more specific needles must come first.
Prefer pravo.by guid=3871 (full text) over publication cards.
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
        "о валютном регулировании и валютном контроле",
        "https://pravo.by/document/?guid=3871&p0=H12200136",
    ),
    (
        "о валютном регулировании",
        "https://pravo.by/document/?guid=3871&p0=H12200136",
    ),
    (
        "об аудиторской деятельности",
        "https://pravo.by/document/?guid=3871&p0=H11300056",
    ),
    (
        "национальные правила аудиторской",
        "https://www.minfin.gov.by/ru/auditor_activities/normative/",
    ),
    (
        "о бухгалтерском учете и отчетности",
        "https://pravo.by/document/?guid=3871&p0=H11300057",
    ),
    (
        "организации системы внутреннего контроля",
        "https://pravo.by/document/?guid=3871&p0=B21326759",
    ),
    (
        "внутреннем контроле при осуществлении банковских операций",
        "https://pravo.by/document/?guid=3871&p0=B21529598",
    ),
    (
        "положение о внутреннем контроле",
        "https://pravo.by/document/?guid=3871&p0=B21326759",
    ),
    (
        "об аренде и безвозмездном пользовании имуществом",
        "https://pravo.by/document/?guid=3871&p0=P32300138",
    ),
    (
        "аренды и безвозмездного пользования имуществом",
        "https://pravo.by/document/?guid=3871&p0=P32300138",
    ),
    (
        "организации ведения бухгалтерского учета",
        "https://pravo.by/document/?guid=3871&p0=B21428262",
    ),
    (
        "ведения бухгалтерского учета в банках",
        "https://pravo.by/document/?guid=3871&p0=B21428262",
    ),
    (
        "плана счетов бухгалтерского учета в банках",
        "https://pravo.by/document/?guid=3871&p0=B21327947",
    ),
    (
        "применения плана счетов бухгалтерского учета",
        "https://pravo.by/document/?guid=3871&p0=B21327947",
    ),
    (
        "регистрации резидентами валютных договоров",
        "https://pravo.by/document/?guid=3871&p0=B22136360",
    ),
]


def lookup_known_url(title: str) -> str | None:
    t = (title or "").lower()
    for needle, url in KNOWN_NPA_URLS:
        if needle in t:
            return url
    return None
