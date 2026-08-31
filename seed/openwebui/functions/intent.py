"""Чистая маршрутизация Pipe «Аудитор»: regex, classify, разбор фраз.

Без httpx и Open WebUI — можно тестировать из backend/tests.
При засеве seed_pipe.py вставляет этот модуль в один paste для Admin → Functions.
"""

from __future__ import annotations

import re
from typing import Any, Optional

APPROVE_RE = re.compile(
    r"(утвержд\w*|подтвержд\w*|выбираю|бери\s+(эти\s+)?акты)",
    re.I,
)
DOWNLOAD_CMD_RE = re.compile(
    r"^\s*(скачай|скачивай|скачать)"
    r"(?:\s+(?:ещ[её]\s+раз|выбранн\w*|акты|документы))?"
    r"\s*[.!]?\s*$",
    re.I,
)
REJECT_APPROVE_RE = re.compile(r"\bне\s+утвержд", re.I)
HEX_ID_RE = re.compile(r"\b([a-f0-9]{8,12})\b", re.I)
EXTRA_MARK_RE = re.compile(
    r"(?:\bплюс\b|\bдобавь(?:те)?\b|\bдополнительно\b|\bи\s+ещ[её]\b|\bещ[её]\s*:|\s\+\s)\s*[:\-–+]?\s*",
    re.I,
)
KB_ASK_RE = re.compile(
    r"^\s*(?:"
    r"вопрос(?:\s+по\s+(?:базе(?:\s+знаний)?|нпа|документам?))?"
    r"|/ask|/вопрос"
    r")\s*[:\-–]?\s*(.*)\s*$",
    re.I | re.S,
)
URL_ATTACH_RE = re.compile(
    r"(?:к|пункт|акт|номер|id)\s*([a-f0-9]{8,12}|\d{1,2})\s+(?:url|ссылка)\s+(https?://\S+)",
    re.I,
)
NEW_CASE_START_RE = re.compile(
    r"^\s*(?:новая\s+проверка|проверка)\b",
    re.I,
)


class Cmd:
    HELP = "help"
    ASK = "ask"
    SELECT_HYPOTHESES = "select_hypotheses"
    OPINION = "opinion"
    CONCLUSION = "conclusion"
    PROGRAM = "program"
    TOTAL = "total"
    HYPOTHESES = "hypotheses"
    BRIEF = "brief"
    APPROVE = "approve"
    LIBRARY = "library"
    STATUS = "status"
    NEW_CASE = "new_case"
    CHAT = "chat"


def _clean_url(url: str) -> str:
    cleaned = (url or "").strip().strip("`\"'<>").rstrip(").,;]")
    if cleaned.endswith("%60"):
        cleaned = cleaned[:-3]
    if "..." in cleaned or "…" in cleaned:
        return ""
    if not cleaned.lower().startswith(("http://", "https://")):
        return ""
    return cleaned


def _is_retry(text: str) -> bool:
    return bool(re.search(r"скач|ещё раз|еще раз|повтор", text, re.I))


def _has_explicit_picks(text: str) -> bool:
    if re.search(r"все\s+обязательн", text, re.I):
        return True
    return bool(re.search(r"утвержд\w*|подтвержд\w*|выбираю", text, re.I)) and bool(
        re.search(r"\b\d{1,2}\b", text)
    )


def _is_program(text: str) -> bool:
    t = text.strip().lower()
    if _is_opinion(t) or _is_conclusion(t):
        return False
    if t in {"программа", "программу", "/program", "audit program"}:
        return True
    if re.match(r"^\s*(программа|программу)\s+\d", t):
        return True
    if re.search(r"(статья|ст\.)\s*\d+", t) and not re.search(r"программ", t):
        return False
    return bool(
        re.search(
            r"(программ\w*\s+(проверк|аудиторск|аудита)|"
            r"аудиторск\w*\s+программ|"
            r"(сделай|составь|подготовь|напиши)\s+программ|"
            r"/program|audit\s+program)",
            t,
        )
    )


def _parse_program_items_spec(text: str) -> Optional[tuple[int, int]]:
    cleaned = re.sub(r"заново|пересобер\w*|перегенер\w*|force", " ", text, flags=re.I)

    def _pair(spec: str) -> tuple[int, int]:
        compact = re.sub(r"\s+", "", spec)
        ranged = re.fullmatch(r"(\d{1,2})[-–—](\d{1,2})", compact)
        if ranged:
            lo, hi = int(ranged.group(1)), int(ranged.group(2))
            if lo > hi:
                lo, hi = hi, lo
            return max(3, min(20, lo)), max(3, min(20, hi))
        n = max(3, min(20, int(compact)))
        return n, n

    match = re.search(
        r"(?:программ\w*|audit\s+program|/program)[^\n\d]{0,80}"
        r"(\d{1,2}\s*[-–—]\s*\d{1,2}|\d{1,2})",
        cleaned,
        re.I,
    )
    if match:
        return _pair(match.group(1))
    match = re.search(
        r"(?:строго|ровно|только)\s+(\d{1,2})(?:\s*[-–—]\s*(\d{1,2}))?\s*пункт",
        cleaned,
        re.I,
    )
    if match:
        spec = match.group(1) if not match.group(2) else f"{match.group(1)}-{match.group(2)}"
        return _pair(spec)
    return None


def _is_total(text: str) -> bool:
    t = text.strip().lower()
    if t in {
        "саммари total",
        "саммари тотал",
        "total саммари",
        "/total",
        "конспект модели",
        "из головы",
    }:
        return True
    return bool(
        re.search(
            r"("
            r"саммари\s+total|"
            r"саммари\s+тотал|"
            r"total\s+саммари|"
            r"сводк\w*\s+total|"
            r"конспект\s+(модели|llm|из\s+голов)|"
            r"из\s+голов\w*\s+модел|"
            r"(обзор|конспект)\s+без\s+баз"
            r")",
            t,
        )
    )


def _is_hypotheses(text: str) -> bool:
    t = text.strip().lower()
    if _is_select_hypotheses(t) or _is_opinion(t) or _is_conclusion(t):
        return False
    if REJECT_APPROVE_RE.search(t):
        return False
    if t in {
        "гипотезы",
        "гипотеза",
        "checklist",
        "чеклист",
        "чеклист гипотез",
        "/hypotheses",
        "/hypothesis",
    }:
        return True
    return bool(
        re.search(
            r"("
            r"гипотез\w*|"
            r"чеклист\s+гипотез|"
            r"(сделай|составь|подготовь|сформулируй)\s+гипотез|"
            r"/hypothes"
            r")",
            t,
        )
    )


def _is_select_hypotheses(text: str) -> bool:
    t = text.strip().lower()
    if not re.search(r"гипотез", t):
        return False
    if REJECT_APPROVE_RE.search(t):
        return False
    return bool(
        re.search(
            r"утвержд\w*|подтвержд\w*|выбираю|"
            r"добав\w*|прикреп\w*|свои\s+гипотез|(?:^|\s)\+?\s*плюс\s+",
            t,
        )
    )


def _is_opinion(text: str) -> bool:
    t = text.strip().lower()
    if t in {
        "аудиторское мнение",
        "мнение аудитора",
        "/opinion",
        "/мнение",
    }:
        return True
    return bool(
        re.search(
            r"("
            r"аудиторск\w*\s+мнен|"
            r"мнен\w*\s+аудитор|"
            r"(сделай|составь|подготовь|напиши)\s+(аудиторск\w*\s+)?мнен|"
            r"/opinion|/мнение"
            r")",
            t,
        )
    )


def _parse_hypothesis_picks(text: str) -> dict[str, Any]:
    t = text.strip().lower()
    if re.search(r"все\s+(с\s+приоритетом\s+)?высок", t):
        return {"all_high": True, "numbers": [], "all_rows": False}
    if re.search(r"все\s+гипотез|утверждаю\s+все(?!\s+обязательн)|подтверждаю\s+все", t):
        return {"all_rows": True, "numbers": [], "all_high": False}
    numbers = [int(n) for n in re.findall(r"\b(\d{1,2})\b", t)]
    numbers = [n for n in numbers if 1 <= n <= 20]
    return {"numbers": numbers, "all_high": False, "all_rows": False}


def _wants_extra_hypotheses(text: str) -> bool:
    t = text.strip().lower()
    return bool(
        re.search(
            r"добав\w*|прикреп\w*|свои\s+гипотез|(?:^|\s)\+?\s*плюс\s+|"
            r"дополнительн\w*\s+гипотез",
            t,
        )
    )


def _parse_opinion_font(text: str) -> str:
    cleaned = re.sub(r"заново|пересобер\w*|перегенер\w*|force", " ", text or "", flags=re.I)
    if re.search(r"(?:^|\s)-c(?:\s|$)|(?<!\w)calibri(?!\w)|калибри", cleaned, re.I):
        return "c"
    if re.search(r"(?:^|\s)-t(?:\s|$)|times(?:\s+new\s+roman)?|таймс", cleaned, re.I):
        return "t"
    return "t"


def _is_conclusion(text: str) -> bool:
    t = text.strip().lower()
    if t in {
        "аудиторское заключение",
        "заключение аудитора",
        "/report",
        "/conclusion",
        "/заключение",
    }:
        return True
    return bool(
        re.search(
            r"("
            r"аудиторск\w*\s+заключен|"
            r"заключен\w*\s+аудитор|"
            r"(сделай|составь|подготовь|напиши)\s+(аудиторск\w*\s+)?заключен|"
            r"/report|/conclusion"
            r")",
            t,
        )
    )


def _is_brief(text: str) -> bool:
    t = text.strip().lower()
    if _is_total(t) or _is_hypotheses(t) or _is_opinion(t) or _is_conclusion(t):
        return False
    if t in {"саммари", "сводка", "бриф", "docx", "word", "/brief", "/summary"}:
        return True
    if re.search(r"(статья|ст\.)\s*\d+", t) and not re.search(r"\bdocx\b|word-файл", t):
        return False
    return bool(
        re.search(
            r"(саммари|сводк\w*|бриф|briefing|\bdocx\b|/brief|/summary|"
            r"обзор\s+(акт|нпа|норм)|word-файл|файл word)",
            t,
        )
    )


def _is_download_retry(text: str) -> bool:
    return bool(DOWNLOAD_CMD_RE.match(text or ""))


def _is_approve(text: str, *, has_case: bool = False) -> bool:
    if REJECT_APPROVE_RE.search(text):
        return False
    if APPROVE_RE.search(text):
        return True
    if URL_ATTACH_RE.search(text):
        return True
    if re.match(
        r"^\s*(добавь(?:те)?|дополнительно|и\s+ещ[её]|ещ[её]\s*:|\+)\b",
        text,
        re.I,
    ):
        return True
    return bool(has_case and _is_download_retry(text))


def _is_library(text: str) -> bool:
    """Команда списка/архива, не любой текст со словом «документ»."""
    t = text.strip().lower()
    if (
        _is_brief(t)
        or _is_program(t)
        or _is_total(t)
        or _is_hypotheses(t)
        or _is_opinion(t)
        or _is_conclusion(t)
        or _is_select_hypotheses(t)
    ):
        return False
    if _is_download_retry(t) or re.search(r"скачай|скачать|скачивай", t):
        return False
    if t in {
        "документы",
        "документ",
        "библиотека",
        "библиотеку",
        "файлы",
        "архив",
        "/library",
    }:
        return True
    return bool(
        re.search(
            r"("
            r"посмотреть\s+(акты|документы)|"
            r"покажи\s+(акты|документы)|"
            r"что\s+скача|"
            r"список\s+документов|"
            r"/library"
            r")",
            t,
        )
    )


def _is_status(text: str) -> bool:
    t = text.strip().lower()
    return t in {"статус", "status", "кейсы", "проверки", "/status"} or t.startswith("статус ")


def _parse_kb_question(text: str) -> Optional[str]:
    """Явный вопрос к базе знаний: «вопрос …» / «вопрос по базе: …» / `/ask …`.

    Возвращает текст вопроса (может быть пустым), или None если это не команда ask.
    """
    match = KB_ASK_RE.match(text.strip())
    if not match:
        return None
    return (match.group(1) or "").strip()


def _parse_new_case(text: str) -> Optional[dict[str, Any]]:
    raw = text.strip()
    if not NEW_CASE_START_RE.match(raw):
        return None
    if len(raw) < 12:
        return None
    if _parse_kb_question(raw) is not None:
        return None
    if (
        _is_approve(raw)
        or _is_status(raw)
        or _is_library(raw)
        or _is_brief(raw)
        or _is_total(raw)
        or _is_program(raw)
        or _is_hypotheses(raw)
        or _is_opinion(raw)
        or _is_conclusion(raw)
        or _is_select_hypotheses(raw)
    ):
        return None
    parts = [p.strip(" .;") for p in re.split(r"[,;\n]", raw) if p.strip()]
    if not parts:
        return None
    return {
        "inspection_name": parts[0],
        "keywords": [part for part in parts[1:] if part],
    }


def _is_help(text: str) -> bool:
    return (not (text or "").strip()) or text.strip().lower() in {
        "помощь",
        "help",
        "/help",
        "?",
    }


def classify(text: str, *, has_case: bool = False) -> str:
    """Ordered command classifier. Order is the product — do not reorder."""
    if _is_help(text):
        return Cmd.HELP
    if _parse_kb_question(text) is not None:
        return Cmd.ASK
    if _is_select_hypotheses(text):
        return Cmd.SELECT_HYPOTHESES
    if _is_opinion(text):
        return Cmd.OPINION
    if _is_conclusion(text):
        return Cmd.CONCLUSION
    if _is_program(text):
        return Cmd.PROGRAM
    if _is_total(text):
        return Cmd.TOTAL
    if _is_hypotheses(text):
        return Cmd.HYPOTHESES
    if _is_brief(text):
        return Cmd.BRIEF
    if _is_approve(text, has_case=has_case):
        return Cmd.APPROVE
    if _is_library(text):
        return Cmd.LIBRARY
    if _is_status(text):
        return Cmd.STATUS
    if not has_case and _parse_new_case(text):
        return Cmd.NEW_CASE
    return Cmd.CHAT


def _resolve_approval(
    text: str, docs: list[dict]
) -> tuple[list[str], dict[str, str], list[str]]:
    main, extras_blob = _split_extra_section(text)
    extras = _parse_extra_titles(extras_blob)
    manuals: dict[str, str] = {}
    for match in URL_ATTACH_RE.finditer(text):
        key, raw_url = match.group(1), match.group(2)
        url = _clean_url(raw_url)
        if not url:
            continue
        doc_id = _index_or_id(key, docs)
        if doc_id:
            manuals[doc_id] = url

    if re.search(r"все\s+обязательн", main, re.I):
        ids = [d["id"] for d in docs if int(d.get("priority") or 2) == 1]
        return ids, manuals, extras

    numbers = [int(n) for n in re.findall(r"\b(\d{1,2})\b", main)]
    ids_from_n = []
    for n in numbers:
        if 1 <= n <= len(docs):
            ids_from_n.append(docs[n - 1]["id"])
    hex_ids = [h.lower() for h in HEX_ID_RE.findall(main)]
    known = {d["id"] for d in docs}
    ids_from_hex = [h for h in hex_ids if h in known]
    merged = list(dict.fromkeys(ids_from_n + ids_from_hex + list(manuals.keys())))
    return merged, manuals, extras


def _split_extra_section(text: str) -> tuple[str, str]:
    match = EXTRA_MARK_RE.search(text)
    if not match:
        return text, ""
    return text[: match.start()].strip(), text[match.end() :].strip()


def _parse_extra_titles(blob: str) -> list[str]:
    raw = (blob or "").strip().strip(" .,:;")
    if not raw:
        return []
    parts = re.split(r"\s*[;\n]\s*|\s+\+\s+", raw)
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        chunk = part.strip()
        and_split = re.search(r"\s+и\s+", chunk, re.I)
        pieces = [chunk]
        if and_split:
            right = chunk[and_split.end() :].strip()
            if re.match(
                r"^(закон|кодекс|инструкц|положен|постановлен|указ|декрет|"
                r"правил|налоговый|гражданский|банковский)",
                right,
                re.I,
            ):
                left = chunk[: and_split.start()].strip()
                pieces = [left, right] if left else [right]
        for piece in pieces:
            title = re.sub(r"^[\d]+[.)]\s*", "", piece).strip(" .,:;")
            title = re.sub(
                r"^(?:и|ещ[её]|также|плюс|добавь(?:те)?|документы?)\s+",
                "",
                title,
                flags=re.I,
            ).strip(" .,:;")
            key = re.sub(r"\s+", " ", title.lower())
            if len(key) < 8 or key in seen:
                continue
            if re.search(
                r"(audit-case|chat_history|если знаете ссылку|вставьте адрес|"
                r"<!--|</?\w+>|https?://|guid=…)",
                title,
                re.I,
            ):
                continue
            if not re.search(
                r"(закон|кодекс|инструкц|положен|постановлен|указ|декрет|"
                r"правил|приказ|письмо|разъяснен|нбрб|минфин|налогов)",
                title,
                re.I,
            ):
                continue
            seen.add(key)
            out.append(title)
    return out


def _index_or_id(key: str, docs: list[dict]) -> Optional[str]:
    if key.isdigit():
        n = int(key)
        if 1 <= n <= len(docs):
            return docs[n - 1]["id"]
        return None
    known = {d["id"] for d in docs}
    return key.lower() if key.lower() in known else None
