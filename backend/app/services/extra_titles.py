"""Auditor-supplied NPA titles that were missing from the LLM propose list."""

from __future__ import annotations

import re

DOC_START_RE = re.compile(
    r"^(закон|кодекс|инструкц|положен|постановлен|указ|декрет|"
    r"правил[ао]|налоговый|гражданский|банковский|письмо|разъяснен)",
    re.I,
)

SPLIT_RE = re.compile(r"\s*[;\n]\s*|\s+\+\s+")
SPACE_RE = re.compile(r"\s+")
QUOTES_RE = re.compile(r"[«»\"„“”']")


def norm_title(title: str) -> str:
    text = QUOTES_RE.sub("", (title or "").lower())
    return SPACE_RE.sub(" ", text).strip(" .,:;")


def split_extra_titles(blob: str) -> list[str]:
    """Split a free-text extras blob into individual act titles."""
    raw = (blob or "").strip().strip(" .,:;")
    if not raw:
        return []
    parts: list[str] = []
    for chunk in SPLIT_RE.split(raw):
        parts.extend(_split_and_conjunction(chunk))
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        title = _clean_title(part)
        key = norm_title(title)
        if len(key) < 8 or key in seen:
            continue
        seen.add(key)
        out.append(title)
    return out


def expand_extra_titles(titles: list[str] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in titles or []:
        for title in split_extra_titles(item):
            key = norm_title(title)
            if key in seen:
                continue
            seen.add(key)
            out.append(title)
    return out


def search_queries_for_title(title: str) -> list[str]:
    t = (title or "").strip()
    if not t:
        return []
    return [
        f"site:pravo.by {t}",
        f"site:pravo.gov.by {t}",
        f"site:etalonline.by {t}",
        f"site:nbrb.by {t}",
        f"site:minfin.gov.by {t}",
        t,
    ]


def guess_doc_type(title: str) -> str:
    t = (title or "").lower()
    mapping = (
        ("кодекс", "кодекс"),
        ("закон", "закон"),
        ("инструкц", "инструкция"),
        ("постановлен", "постановление"),
        ("указ", "указ"),
        ("декрет", "декрет"),
        ("положен", "положение"),
        ("правил", "правила"),
    )
    for needle, label in mapping:
        if needle in t:
            return label
    return "иное"


def _clean_title(part: str) -> str:
    text = (part or "").strip()
    text = re.sub(r"^[\d]+[.)]\s*", "", text)
    text = re.sub(
        r"^(?:и|ещ[её]|также|плюс|добавь(?:те)?|документы?)\s+",
        "",
        text,
        flags=re.I,
    )
    return text.strip(" .,:;")


def _split_and_conjunction(chunk: str) -> list[str]:
    text = (chunk or "").strip()
    if not text:
        return []
    match = re.search(r"\s+и\s+", text, re.I)
    if not match:
        return [text]
    right = text[match.end() :].strip()
    if DOC_START_RE.match(right):
        left = text[: match.start()].strip()
        return [left, right] if left else [right]
    return [text]
