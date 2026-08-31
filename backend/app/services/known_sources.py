"""Curated official NPA entry points (Belarus).

Used when public search engines are empty or rate-limited.
Each entry is a list of lowercase title needles → preferred official URL.
Longer / more specific needles must come first.
Prefer pravo.by guid=3871 (full text) over publication cards.
"""

from __future__ import annotations

import re

from app.services.npa_identity import norm as _norm
from app.services.npa_identity import stems as _stems

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

_CODE_IN_URL = re.compile(
    r"(?:[?&]p0=|[?&]regnum=|[?&]RN=|/op/)([A-Za-z][A-Za-z0-9]{5,24})",
    re.I,
)

CODE_TO_URL: dict[str, str] = {}
URL_TO_NEEDLES: dict[str, tuple[str, ...]] = {}
for needles, url in KNOWN_NPA:
    URL_TO_NEEDLES[url] = needles
    match = _CODE_IN_URL.search(url)
    if match:
        CODE_TO_URL[match.group(1).lower()] = url

# Short names auditors actually say. Checked before "this code belongs to another act".
_SHORT_HINTS = {
    "hk9800218": ("гражданск", " гк ", "гк рб"),
    "hk0200166": ("налогов", " нк ", "нк рб"),
    "hk0000441": ("банковск",),
}


def _code_of(url: str | None) -> str | None:
    if not url:
        return None
    match = _CODE_IN_URL.search(url)
    return match.group(1).lower() if match else None


def _needle_score(title_stems: set[str], needle: str) -> float:
    needle_stems = _stems(needle)
    if len(needle_stems) < 2:
        return 0.0
    overlap = title_stems & needle_stems
    if not overlap:
        return 0.0
    precision = len(overlap) / len(needle_stems)
    recall = len(overlap) / max(len(title_stems), 1)
    if precision < 0.8:
        return 0.0
    if len(overlap) < min(3, len(needle_stems)):
        return 0.0
    return 2 * precision * recall / (precision + recall)


def lookup_known_url(title: str) -> str | None:
    t = _norm(title)
    if not t:
        return None
    for needle, url in KNOWN_NPA_URLS:
        if needle in t:
            return url

    title_stems = _stems(title)
    if len(title_stems) < 2:
        return None
    ranked: list[tuple[float, int, str]] = []
    for needle, url in KNOWN_NPA_URLS:
        score = _needle_score(title_stems, needle)
        if score < 0.55:
            continue
        ranked.append((score, len(needle), url))
    if not ranked:
        return None
    ranked.sort(reverse=True)
    best_score, _best_len, best_url = ranked[0]
    rival = next((row for row in ranked[1:] if row[2] != best_url), None)
    if rival and best_score - rival[0] < 0.08:
        return None
    return best_url


def url_code_conflicts_title(code: str | None, title: str) -> bool:
    """True when `code` is a catalogued act that is not the one named by `title`."""
    if not code:
        return False
    code = code.lower()
    owned = CODE_TO_URL.get(code)
    if not owned:
        return False
    blob = f" {_norm(title)} "
    for hint in _SHORT_HINTS.get(code, ()):
        if hint in blob or hint.strip() in blob:
            return False
    expected = lookup_known_url(title)
    if expected:
        exp = _code_of(expected)
        return bool(exp) and exp != code
    needles = URL_TO_NEEDLES.get(owned) or ()
    t = _norm(title)
    if any(needle in t for needle in needles):
        return False
    title_stems = _stems(title)
    for needle in needles:
        if _needle_score(title_stems, needle) >= 0.55:
            return False
    return True
