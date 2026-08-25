"""Curated official NPA entry points (Belarus).

Used when public search engines are empty or rate-limited.
Each entry is a list of lowercase title needles → preferred official URL.
Longer / more specific needles must come first.
Prefer pravo.by guid=3871 (full text) over publication cards.
"""

from __future__ import annotations

import re

# needles (lowercase) -> preferred official URL
KNOWN_NPA: list[tuple[tuple[str, ...], str]] = [
    (
        ("гражданский кодекс",),
        "https://pravo.by/document/?guid=3871&p0=hk9800218",
    ),
    (
        ("налоговый кодекс",),
        "https://pravo.by/document/?guid=3871&p0=Hk0200166",
    ),
    (
        ("банковский кодекс",),
        "https://pravo.by/document/?guid=3871&p0=Hk0000441",
    ),
    (
        (
            "о валютном регулировании и валютном контроле",
            "о валютном регулировании",
        ),
        "https://etalonline.by/document/?regnum=H12200136",
    ),
    (
        ("об аудиторской деятельности",),
        "https://pravo.by/document/?guid=3871&p0=H11300056",
    ),
    (
        ("национальные правила аудиторской",),
        "https://www.minfin.gov.by/ru/auditor_activities/normative/",
    ),
    (
        ("о бухгалтерском учете и отчетности",),
        "https://pravo.by/document/?guid=3871&p0=H11300057",
    ),
    (
        (
            "бухгалтерском учете финансовой аренды",
            "бухгалтерском учете аренды",
            "учете финансовой аренды",
            "финансовая аренда (лизинг)",
        ),
        "https://pravo.by/document/?guid=3871&p0=W21833716",
    ),
    (
        (
            "бухгалтерскому учету операций с имуществом и аренды",
            "учету операций с имуществом и аренды",
        ),
        "https://pravo.by/document/?guid=3871&p0=B22340032",
    ),
    (
        (
            "организации системы внутреннего контроля",
            "положение о внутреннем контроле",
            "внутреннего аудита в банках",
            "проведения внутреннего аудита",
            "проверок внутреннего аудита",
        ),
        "https://etalonline.by/document/?regnum=B21326759",
    ),
    (
        (
            "внутреннем контроле при осуществлении банковских операций",
            "внутреннему контролю за проведением банковских",
            "требованиях к внутреннему контролю",
            "требованиям к внутреннему контролю",
        ),
        "https://etalonline.by/document/?regnum=B21529598",
    ),
    (
        (
            "об аренде и безвозмездном пользовании имуществом",
            "аренды и безвозмездного пользования имуществом",
            "арендных отношений в сфере недвижимости",
            "регулирования арендных отношений",
            "регулирование арендных отношений",
        ),
        "https://pravo.by/document/?guid=3871&p0=P32300138",
    ),
    (
        (
            "организации ведения бухгалтерского учета",
            "ведения бухгалтерского учета в банках",
            "оформления и хранения банковских документов",
            "оформление и хранение банковских документов",
            "формирование и хранение документов",
        ),
        "https://etalonline.by/document/?regnum=B21428262",
    ),
    (
        (
            "плана счетов бухгалтерского учета в банках",
            "применения плана счетов бухгалтерского учета",
        ),
        "https://pravo.by/document/?guid=12551&p0=B21327947",
    ),
    (
        ("регистрации резидентами валютных договоров",),
        "https://pravo.by/document/?guid=3871&p0=B22136360",
    ),
]

# Flattened for tests / substring lookup: longer needles first.
KNOWN_NPA_URLS: list[tuple[str, str]] = []
for needles, url in KNOWN_NPA:
    for needle in needles:
        KNOWN_NPA_URLS.append((needle, url))
KNOWN_NPA_URLS.sort(key=lambda row: len(row[0]), reverse=True)

_QUOTE_RE = re.compile(r"[«»\"„“”'`]+")
_SPACE_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[а-яёa-z0-9]{5,}")
_STOP = {
    "республики",
    "беларусь",
    "беларуси",
    "утвержден",
    "утверждено",
    "утверждении",
    "некоторых",
    "вопросах",
    "порядке",
    "проведения",
    "национальной",
    "национального",
    "инструкция",
    "положение",
    "постановление",
}


def _norm(text: str) -> str:
    text = _QUOTE_RE.sub(" ", (text or "").lower())
    return _SPACE_RE.sub(" ", text).strip(" .,:;`")


def _stems(text: str) -> set[str]:
    words = _WORD_RE.findall(_norm(text))
    return {w[:6] for w in words if w not in _STOP}


def lookup_known_url(title: str) -> str | None:
    t = _norm(title)
    if not t:
        return None
    for needle, url in KNOWN_NPA_URLS:
        if needle in t:
            return url

    title_stems = _stems(title)
    if len(title_stems) < 3:
        return None
    ranked: list[tuple[int, int, str]] = []
    for needle, url in KNOWN_NPA_URLS:
        needle_stems = _stems(needle)
        if len(needle_stems) < 3:
            continue
        overlap = title_stems & needle_stems
        if len(overlap) < 3:
            continue
        if len(overlap) / len(needle_stems) < 0.7:
            continue
        ranked.append((len(overlap), len(needle), url))
    if not ranked:
        return None
    ranked.sort(reverse=True)
    return ranked[0][2]
