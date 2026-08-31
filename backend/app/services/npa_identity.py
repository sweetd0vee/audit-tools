"""Match an auditor-picked NPA title to search hits and downloaded pages.

Search and curated URLs often return a neighbour act (another code, another
№, the Civil Code instead of a lease instruction). These helpers decide
whether two titles or a title and a page are the same document.
"""

from __future__ import annotations

import re

_QUOTE_RE = re.compile(r"[«»\"„“”'`]+")
_SPACE_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[а-яёa-z0-9]{5,}")
_NUM_RE = re.compile(r"№\s*(\d{1,4}(?:-[а-яёa-z]{1,3})?)", re.I)
_QUOTED_RE = re.compile(r"[«\"„](.+?)[»\"“]")
_GENERIC_HEAD_RE = re.compile(
    r"^(?:закон|кодекс|инструкци\w*|положени\w*|постановлени\w*|указ|декрет|правил[ао])"
    r"(?:\s+республики\s+беларусь)?"
    r"(?:\s+от\s+\d{1,2}\.\d{1,2}\.\d{4})?"
    r"(?:\s*№\s*\S+)?"
    r"\s*",
    re.I,
)
_GENERIC_TITLE_RE = re.compile(
    r"^(?:закон|кодекс)\s+республики\s+беларусь$",
    re.I,
)
_CODE_ALIAS_HEAD = (
    (("гк", "гк рб"), re.compile(r"гражданский\s+кодекс", re.I)),
    (("нк", "нк рб"), re.compile(r"налоговый\s+кодекс", re.I)),
    (("бк", "бк рб"), re.compile(r"банковский\s+кодекс", re.I)),
)
_ACT_KIND_RE = re.compile(
    r"кодекс|закон|инструкц|положен|постановлен|указ|декрет|правил",
    re.I,
)
_TITLED_ACT_NUM_RE = re.compile(
    r"(закон|инструкц|постановлен|указ|положен|декрет|правил).{0,80}№\s*\d+",
    re.I,
)

STOP = {
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
    "национальный",
    "инструкция",
    "инструкции",
    "положение",
    "положения",
    "постановление",
    "постановления",
    "закон",
    "кодекс",
}


def norm(text: str) -> str:
    text = _QUOTE_RE.sub(" ", (text or "").lower())
    return _SPACE_RE.sub(" ", text).strip(" .,:;`")


def stems(text: str) -> set[str]:
    words = _WORD_RE.findall(norm(text))
    out: set[str] = set()
    for word in words:
        if word in STOP:
            continue
        out.add(word[:5])
    return out


def quoted_core(title: str) -> str:
    match = _QUOTED_RE.search(title or "")
    if match:
        return norm(match.group(1))
    stripped = _GENERIC_HEAD_RE.sub("", (title or "").strip())
    return norm(stripped)


def identity_numbers(text: str, *, head_chars: int | None = 180) -> set[str]:
    blob = text or ""
    if head_chars is not None:
        blob = blob[:head_chars]
    return {m.group(1).lower() for m in _NUM_RE.finditer(blob)}


def _generic_prefix_only(title: str) -> bool:
    return bool(_GENERIC_TITLE_RE.match(norm(title)))


def same_npa_title(left: str, right: str) -> bool:
    """True when two auditor/LLM titles name the same act."""
    a, b = norm(left), norm(right)
    if not a or not b:
        return False
    if a == b:
        return True
    left_n, right_n = identity_numbers(left), identity_numbers(right)
    if left_n and right_n and not (left_n & right_n):
        return False
    qa, qb = quoted_core(left), quoted_core(right)
    if qa and qb:
        if qa == qb or (len(qa) >= 12 and (qa in qb or qb in qa)):
            return True
        sa, sb = stems(qa), stems(qb)
        if sa and sb:
            overlap = sa & sb
            if overlap and len(overlap) / max(len(sa), len(sb), 1) >= 0.7:
                return True
            return False
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) >= 24 and shorter in longer:
        if _generic_prefix_only(shorter):
            return False
        core = quoted_core(shorter)
        if not core or _generic_prefix_only(core):
            return False
        return True
    sa, sb = stems(left), stems(right)
    if sa and sa == sb:
        return True
    return False


def titles_compatible(requested: str, other: str) -> bool:
    """Whether a search snippet/heading can be the requested act.

    Short or empty `other` is treated as unknown — do not reject.
    """
    if not (other or "").strip() or not (requested or "").strip():
        return True
    if same_npa_title(requested, other):
        return True
    req_n, oth_n = identity_numbers(requested), identity_numbers(other)
    if req_n and oth_n and not (req_n & oth_n):
        return False
    other_stems = stems(other)
    looks_like_act = bool(_ACT_KIND_RE.search(other or ""))
    if len(other_stems) < 2 and not looks_like_act:
        return True
    req_stems = stems(requested)
    overlap = req_stems & other_stems
    rq, oq = quoted_core(requested), quoted_core(other)
    if rq and oq and len(oq) >= 12:
        if oq in rq or rq in oq:
            return True
        qs, os_ = stems(rq), stems(oq)
        if qs and os_ and qs.isdisjoint(os_):
            return False
    if not overlap:
        return False
    precision = len(overlap) / len(other_stems)
    recall = len(overlap) / max(len(req_stems), 1)
    if len(other_stems) >= 3 and precision < 0.35 and recall < 0.35:
        return False
    return True


def page_matches_title(title: str, page_text: str) -> bool:
    """True when extracted page text is the act named by `title`."""
    if not (title or "").strip():
        return False
    text = page_text or ""
    if len(text.strip()) < 200:
        return False
    head = text[:5000]
    title_stems = stems(title)

    req_n = identity_numbers(title)
    page_n = identity_numbers(head, head_chars=2000)
    if req_n and page_n and not (req_n & page_n) and _TITLED_ACT_NUM_RE.search(head):
        return False

    core = quoted_core(title)
    core_stems = stems(core) if core else title_stems
    if not core_stems:
        compact = norm(title)
        for aliases, pattern in _CODE_ALIAS_HEAD:
            if compact in aliases:
                return bool(pattern.search(head[:3000]))
        return False
    page_stems = stems(head)
    overlap = core_stems & page_stems
    needed = 2 if len(core_stems) >= 2 else 1
    if len(overlap) < min(needed, len(core_stems)):
        return False
    if len(core_stems) >= 3 and len(overlap) / len(core_stems) < 0.45:
        return False
    return True
