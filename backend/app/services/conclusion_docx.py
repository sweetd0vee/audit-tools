from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

from app.services.brief_docx import (
    _add_opinion_paragraph,
    _set_cell_width,
    _set_document_base_font,
    _set_run_font,
    _set_table_borders,
    _set_tbl_grid,
    _strip_md,
    add_opinion_markdown,
)

_FONT_SIZE = 14
_TITLE = "Аудиторское заключение (черновик)"
_TOC_HEADING = "Разделы аудиторского заключения"
_SECTION_I = "Аудиторское мнение по итогам проверки."
_SECTION_II = "Основные результаты аудита и итоговые аудиторские рекомендации."
_SECTION_LAST = "Общая информация об аудиторской проверке."
_ROMAN = [
    "",
    "I",
    "II",
    "III",
    "IV",
    "V",
    "VI",
    "VII",
    "VIII",
    "IX",
    "X",
    "XI",
    "XII",
    "XIII",
    "XIV",
    "XV",
]
_MATERIALITY = {"высокий", "средний", "низкий"}
_HEADING_PREFIX = re.compile(r"^#{1,6}\s*")
_SECTION_RE = re.compile(
    r"^(?:Раздел\s+)?([IVX]{1,6})\.?\s*(.*)$",
    re.I,
)
_OBS_RE = re.compile(
    r"^Наблюдение\s+(\d+(?:\.\d+)?)\.?\s*(.*)$",
    re.I,
)
_MATERIALITY_RE = re.compile(
    r"^(?:уровень\s+)?существенности:\s*(высокий|средний|низкий)\b",
    re.I,
)
_HYP_RE = re.compile(r"^гипотеза:\s*(\d+)\b", re.I)
_REC_RE = re.compile(r"^(?:аудиторская\s+)?рекомендация:\s*(.*)$", re.I)
_SKIP_FIELD_RE = re.compile(
    r"^(аудитор|объект аудита|руководитель объекта|срок)\b",
    re.I,
)
_GENERAL_LABELS = (
    "Основание проведения аудита",
    "Срок проведения",
    "Аудируемый период",
    "Группа аудиторов",
    "Вид аудита",
    "Дата составления заключения",
)


@dataclass
class Observation:
    number: str
    title: str
    body: str
    materiality: str
    recommendation: str
    hypothesis_n: str = ""


@dataclass
class ReportSection:
    roman: str
    title: str
    intro: str = ""
    observations: list[Observation] = field(default_factory=list)
    general_items: list[tuple[str, str]] = field(default_factory=list)
    kind: str = "observations"


@dataclass
class ConclusionDocument:
    sections: list[ReportSection] = field(default_factory=list)


def roman_numeral(n: int) -> str:
    if 1 <= n < len(_ROMAN):
        return _ROMAN[n]
    return str(n)


def normalize_materiality(value: str | None, fallback: str = "средний") -> str:
    raw = (value or "").strip().lower()
    if raw in _MATERIALITY:
        return raw
    if "высок" in raw:
        return "высокий"
    if "низк" in raw:
        return "низкий"
    if "средн" in raw:
        return "средний"
    fb = (fallback or "").strip().lower()
    if fb in _MATERIALITY:
        return fb
    if "высок" in fb:
        return "высокий"
    if "низк" in fb:
        return "низкий"
    return "средний"


def materiality_from_priority(priority: str | None) -> str:
    return normalize_materiality(priority, "средний")


def _bare(line: str) -> str:
    return _HEADING_PREFIX.sub("", (line or "").strip())


def _is_toc_heading(text: str) -> bool:
    low = text.lower()
    return "содержан" in low or "разделы аудиторского заключения" in low


def _is_section_i(title: str) -> bool:
    return "аудиторское мнение" in (title or "").lower()


def _is_section_ii(title: str) -> bool:
    return "основные результаты" in (title or "").lower()


def _is_general(title: str) -> bool:
    return "общая информация" in (title or "").lower()


def _clean_paragraphs(text: str) -> str:
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines).strip()


def default_general_items(period: str | None) -> list[tuple[str, str]]:
    period_s = (period or "").strip() or "уточняется"
    return [
        (
            "Основание проведения аудита",
            "уточняется (приказ / план работы службы внутреннего аудита — заполняет аудитор)",
        ),
        ("Срок проведения", "уточняется"),
        ("Аудируемый период", period_s),
        ("Группа аудиторов", "заполняет аудитор"),
        ("Вид аудита", "Тематическая аудиторская проверка"),
        ("Дата составления заключения", "заполняет аудитор"),
    ]


def _parse_observation_block(
    number: str,
    title: str,
    lines: list[str],
    *,
    fallback_materiality: str = "средний",
) -> Observation:
    materiality = fallback_materiality
    hypothesis_n = ""
    rec_lines: list[str] = []
    body_lines: list[str] = []
    in_rec = False
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            if in_rec:
                rec_lines.append("")
            elif body_lines:
                body_lines.append("")
            continue
        bare = _bare(stripped)
        match_m = _MATERIALITY_RE.match(bare)
        if match_m:
            materiality = normalize_materiality(match_m.group(1), fallback_materiality)
            continue
        match_h = _HYP_RE.match(bare)
        if match_h:
            hypothesis_n = match_h.group(1)
            continue
        match_r = _REC_RE.match(bare)
        if match_r:
            in_rec = True
            rest = (match_r.group(1) or "").strip()
            if rest:
                rec_lines.append(rest)
            continue
        if _SKIP_FIELD_RE.match(bare):
            continue
        if in_rec:
            rec_lines.append(stripped)
        else:
            body_lines.append(raw)
    title_clean = _strip_md(title).strip(" .")
    return Observation(
        number=number,
        title=title_clean,
        body=_clean_paragraphs("\n".join(body_lines)),
        materiality=normalize_materiality(materiality, fallback_materiality),
        recommendation=_clean_paragraphs("\n".join(rec_lines)),
        hypothesis_n=hypothesis_n,
    )


def _parse_general_items(lines: list[str], period: str | None) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    current_label = ""
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_label, current_lines
        if current_label:
            items.append(
                (current_label, _clean_paragraphs("\n".join(current_lines)) or "уточняется")
            )
        current_label = ""
        current_lines = []

    for raw in lines:
        stripped = raw.strip()
        bare = _bare(stripped)
        label = next((lab for lab in _GENERAL_LABELS if bare.lower().startswith(lab.lower())), None)
        if label:
            flush()
            current_label = label
            rest = bare[len(label) :].lstrip(" .:–-")
            current_lines = [rest] if rest else []
            continue
        if current_label:
            current_lines.append(raw)
    flush()
    if not items:
        return default_general_items(period)
    have = {label.lower() for label, _ in items}
    for label, value in default_general_items(period):
        if label.lower() not in have:
            items.append((label, value))
    return items


def fallback_from_hypotheses(
    hypotheses: list[dict[str, str]],
    *,
    leftover: str = "",
    period: str | None = None,
) -> ConclusionDocument:
    observations: list[Observation] = []
    leftover_body = _clean_paragraphs(leftover)
    for i, row in enumerate(hypotheses, start=1):
        n = str(row.get("n") or i)
        title = (row.get("hypothesis") or f"Гипотеза {n}").strip()
        if len(title) > 140:
            title = title[:137].rstrip() + "…"
        parts: list[str] = []
        if leftover_body and i == 1:
            parts.append(leftover_body)
        else:
            hyp = (row.get("hypothesis") or "").strip()
            if hyp:
                parts.append(
                    "По итогам рассмотрения подтверждённой гипотезы отмечается следующий "
                    "риск / недостаток контроля. " + hyp.rstrip(".") + "."
                )
            risk = (row.get("risk") or "").strip()
            why = (row.get("why_risk") or "").strip()
            if risk or why:
                parts.append(" ".join(p for p in (risk, why) if p))
            how = (row.get("how_to_test") or "").strip()
            npa = (row.get("npa_criteria") or "").strip()
            extra: list[str] = []
            if how:
                extra.append(f"В ходе проверки целесообразно опереться на процедуру: {how.rstrip('.')}.")
            if npa:
                extra.append(f"Критерии: {npa.rstrip('.')}.")
            if extra:
                parts.append(" ".join(extra))
        rec = (row.get("working_paper") or "").strip()
        if not rec:
            rec = (
                "Проработать выявленный риск по подтверждённой гипотезе и закрепить "
                "контрольные процедуры в локальных актах и операционном контуре банка."
            )
        observations.append(
            Observation(
                number=f"3.{i}",
                title=title.rstrip("."),
                body=_clean_paragraphs("\n\n".join(parts)),
                materiality=materiality_from_priority(row.get("priority")),
                recommendation=rec,
                hypothesis_n=n,
            )
        )
    return ConclusionDocument(
        sections=[
            ReportSection(
                roman="III",
                title="Наблюдения аудитора в ходе проверки",
                observations=observations,
                kind="observations",
            ),
            ReportSection(
                roman="IV",
                title=_SECTION_LAST.rstrip("."),
                general_items=default_general_items(period),
                kind="general",
            ),
        ]
    )


def parse_conclusion_markdown(
    md: str,
    *,
    hypotheses: list[dict[str, str]] | None = None,
    period: str | None = None,
) -> ConclusionDocument:
    hypotheses = hypotheses or []
    lines = (md or "").splitlines()
    blocks: list[tuple[str, str, str, list[str]]] = []
    kind, code, title, buf = "lead", "", "", []

    def push() -> None:
        if kind != "lead" or any(ln.strip() for ln in buf):
            blocks.append((kind, code, title, list(buf)))

    for raw in lines:
        stripped = raw.strip()
        bare = _bare(stripped)
        if _is_toc_heading(bare):
            push()
            kind, code, title, buf = "toc", "", bare, []
            continue
        obs = _OBS_RE.match(bare)
        if obs:
            push()
            kind, code, title, buf = "obs", obs.group(1), (obs.group(2) or "").strip(), []
            continue
        sec = _SECTION_RE.match(bare)
        looks_heading = (
            stripped.startswith("#")
            or bare.lower().startswith("раздел ")
            or (sec and sec.group(1).isupper() and (stripped.startswith("#") or len(bare) < 120))
        )
        if sec and looks_heading:
            roman = sec.group(1).upper()
            rest = (sec.group(2) or "").strip()
            if roman in {"I", "II"} or _is_section_i(rest) or _is_section_ii(rest):
                push()
                kind, code, title, buf = "skip", roman, rest, []
                continue
            if kind == "toc" and not stripped.startswith("#") and not bare.lower().startswith("раздел "):
                buf.append(raw)
                continue
            push()
            kind, code, title, buf = "sec", roman, rest, []
            continue
        buf.append(raw)
    push()

    sections: list[ReportSection] = []
    current: ReportSection | None = None
    leftovers: list[str] = []
    used_hyps = 0

    def close_current() -> None:
        nonlocal current
        if current is None:
            return
        if current.kind == "observations" and not current.observations and current.intro:
            current.observations.append(
                Observation(
                    number=f"{_roman_to_int(current.roman)}.1",
                    title=current.title,
                    body=current.intro,
                    materiality="средний",
                    recommendation="",
                )
            )
            current.intro = ""
        sections.append(current)
        current = None

    for kind, code, title, body_lines in blocks:
        if kind in {"lead", "toc", "skip"}:
            leftovers.extend(body_lines)
            continue
        if kind == "sec":
            close_current()
            if _is_general(title):
                current = ReportSection(
                    roman=code or "IV",
                    title=title or _SECTION_LAST.rstrip("."),
                    general_items=_parse_general_items(body_lines, period),
                    kind="general",
                )
                close_current()
                continue
            current = ReportSection(
                roman=code or roman_numeral(len(sections) + 3),
                title=title or "Наблюдения аудитора в ходе проверки",
                intro=_clean_paragraphs("\n".join(body_lines)),
                kind="observations",
            )
            continue
        if kind == "obs":
            if current is None or current.kind != "observations":
                close_current()
                roman = code.split(".", 1)[0] if "." in code else roman_numeral(len(sections) + 3)
                current = ReportSection(
                    roman=roman_numeral(int(roman)) if roman.isdigit() else roman,
                    title="Наблюдения аудитора в ходе проверки",
                    kind="observations",
                )
            fallback = "средний"
            if used_hyps < len(hypotheses):
                fallback = materiality_from_priority(hypotheses[used_hyps].get("priority"))
                used_hyps += 1
            current.observations.append(
                _parse_observation_block(code, title, body_lines, fallback_materiality=fallback)
            )
    close_current()

    observation_sections = [s for s in sections if s.kind == "observations" and s.observations]
    if not observation_sections:
        leftover_text = _clean_paragraphs("\n".join(leftovers))
        return fallback_from_hypotheses(hypotheses, leftover=leftover_text, period=period)

    if not any(s.kind == "general" for s in sections):
        last_n = _roman_to_int(observation_sections[-1].roman) + 1
        sections.append(
            ReportSection(
                roman=roman_numeral(last_n),
                title=_SECTION_LAST.rstrip("."),
                general_items=default_general_items(period),
                kind="general",
            )
        )
    _renumber_sections(sections)
    return ConclusionDocument(sections=sections)


def _roman_to_int(value: str) -> int:
    raw = (value or "").upper().strip()
    if raw.isdigit():
        return int(raw)
    try:
        return _ROMAN.index(raw)
    except ValueError:
        return 3


def _renumber_sections(sections: list[ReportSection]) -> None:
    n = 3
    for section in sections:
        if section.kind == "observations":
            section.roman = roman_numeral(n)
            for i, obs in enumerate(section.observations, start=1):
                obs.number = f"{n}.{i}"
            n += 1
        elif section.kind == "general":
            section.roman = roman_numeral(n)


def toc_entries(doc: ConclusionDocument) -> list[tuple[str, str]]:
    entries = [("I", _SECTION_I), ("II", _SECTION_II)]
    for section in doc.sections:
        title = section.title.strip().rstrip(".")
        if section.kind == "general":
            title = _SECTION_LAST
        else:
            title = title + "."
        entries.append((section.roman, title))
    if not any("общая информация" in title.lower() for _, title in entries):
        entries.append((roman_numeral(len(entries) + 1), _SECTION_LAST))
    return entries


def _add_runs(paragraph, parts: list[tuple[str, dict]], *, font: str) -> None:
    for text, opts in parts:
        if not text:
            continue
        run = paragraph.add_run(text)
        _set_run_font(
            run,
            size=int(opts.get("size", _FONT_SIZE)),
            bold=bool(opts.get("bold")),
            italic=bool(opts.get("italic")),
            font=font,
        )
        if opts.get("underline"):
            run.underline = True


def _add_styled_paragraph(
    doc: Document,
    *,
    font: str,
    align: str = "justify",
    space_before: int = 0,
    space_after: int = 8,
    first_line: bool = False,
) -> object:
    _ = font
    paragraph = doc.add_paragraph()
    fmt = paragraph.paragraph_format
    fmt.space_before = Pt(space_before)
    fmt.space_after = Pt(space_after)
    fmt.line_spacing_rule = WD_LINE_SPACING.ONE_POINT_FIVE
    if align == "center":
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        fmt.first_line_indent = Cm(0)
    elif align == "left":
        paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
        fmt.first_line_indent = Cm(0)
    else:
        paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        fmt.first_line_indent = Cm(1.25) if first_line else Cm(0)
    return paragraph


def _blank(doc: Document, *, font: str, after: int = 0) -> None:
    p = _add_styled_paragraph(doc, font=font, align="left", space_after=after)
    run = p.add_run("")
    _set_run_font(run, size=_FONT_SIZE, font=font)


def _add_body_markdown(doc: Document, md: str, *, font: str) -> None:
    for raw in (md or "").splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("- ") or stripped.startswith("* "):
            _add_opinion_paragraph(
                doc,
                stripped[2:].strip(),
                font=font,
                size=_FONT_SIZE,
                first_line=False,
                bullet=True,
                space_after=4,
            )
            continue
        _add_opinion_paragraph(doc, stripped, font=font, size=_FONT_SIZE)


def _set_row_height(row, twips: int) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    existing = tr_pr.find(qn("w:trHeight"))
    if existing is not None:
        tr_pr.remove(existing)
    el = OxmlElement("w:trHeight")
    el.set(qn("w:val"), str(twips))
    tr_pr.append(el)


def _set_tbl_center(table) -> None:
    tbl_pr = table._tbl.tblPr
    if tbl_pr is None:
        tbl_pr = OxmlElement("w:tblPr")
        table._tbl.insert(0, tbl_pr)
    jc = tbl_pr.find(qn("w:jc"))
    if jc is None:
        jc = OxmlElement("w:jc")
        tbl_pr.append(jc)
    jc.set(qn("w:val"), "center")
    table.alignment = WD_TABLE_ALIGNMENT.CENTER


def _add_observation_box(doc: Document, observation: Observation, *, font: str) -> None:
    table = doc.add_table(rows=1, cols=2)
    _set_tbl_grid(table, [4.5, 13.0])
    _set_table_borders(table, val="single", sz="4", color="000000")
    _set_tbl_center(table)
    row = table.rows[0]
    _set_row_height(row, 795)
    left, right = row.cells
    _set_cell_width(left, 4.5)
    _set_cell_width(right, 13.0)
    left.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
    right.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER

    lp = left.paragraphs[0]
    lp.alignment = WD_ALIGN_PARAGRAPH.LEFT
    lp.paragraph_format.space_after = Pt(0)
    lp.paragraph_format.space_before = Pt(0)
    run = lp.add_run(f"Наблюдение {observation.number}. ")
    _set_run_font(run, size=_FONT_SIZE, font=font)

    rp = right.paragraphs[0]
    rp.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    rp.paragraph_format.space_after = Pt(0)
    rp.paragraph_format.space_before = Pt(0)
    title_run = rp.add_run(observation.title or "Название наблюдения")
    _set_run_font(title_run, size=_FONT_SIZE, italic=True, font=font)
    title_run.underline = True

    materiality = _add_styled_paragraph(
        doc, font=font, align="justify", space_before=8, space_after=4, first_line=False
    )
    _add_runs(
        materiality,
        [
            ("Уровень существенности:", {}),
            (f" {observation.materiality}", {"bold": True}),
        ],
        font=font,
    )

    auditor = _add_styled_paragraph(doc, font=font, align="justify", space_after=0, first_line=False)
    _add_runs(
        auditor,
        [("\t", {"bold": True, "italic": True}), ("Аудитор:", {})],
        font=font,
    )

    obj = _add_styled_paragraph(doc, font=font, align="justify", space_after=0, first_line=False)
    _add_runs(obj, [("          ", {"bold": True}), ("Объект аудита: ", {})], font=font)

    head = _add_styled_paragraph(doc, font=font, align="justify", space_after=8, first_line=False)
    _add_runs(head, [("Руководитель объекта аудита: ", {})], font=font)

    _blank(doc, font=font)

    rec_label = _add_styled_paragraph(doc, font=font, align="justify", space_after=4, first_line=False)
    _add_runs(rec_label, [("          Аудиторская рекомендация: ", {"bold": True})], font=font)

    if observation.recommendation:
        _add_opinion_paragraph(
            doc,
            observation.recommendation,
            font=font,
            size=_FONT_SIZE,
            first_line=True,
            space_after=4,
        )
    deadline = _add_styled_paragraph(doc, font=font, align="justify", space_after=12, first_line=False)
    _add_runs(deadline, [("Срок – ", {"bold": True})], font=font)
    _blank(doc, font=font)


def _add_section_heading(doc: Document, roman: str, *, font: str, title: str = "") -> None:
    p = _add_styled_paragraph(doc, font=font, align="left", space_before=12, space_after=8, first_line=False)
    _add_runs(
        p,
        [("Раздел ", {"bold": True, "size": 16}), (f"{roman}.", {"bold": True, "size": 16})],
        font=font,
    )
    if title and "общая информация" not in title.lower():
        t = _add_styled_paragraph(doc, font=font, align="justify", space_after=8, first_line=False)
        _add_runs(t, [(title.rstrip(".") + ".", {"bold": True})], font=font)


def _write_title_page(
    doc: Document,
    *,
    inspection_name: str,
    period: str | None,
    font: str,
) -> None:
    for _ in range(4):
        _blank(doc, font=font, after=0)
    dept = _add_styled_paragraph(doc, font=font, align="center", space_after=6)
    _add_runs(dept, [("Департамент внутреннего аудита", {"size": 14})], font=font)
    for _ in range(6):
        _blank(doc, font=font, after=0)
    title = _add_styled_paragraph(doc, font=font, align="center", space_after=4)
    _add_runs(title, [("АУДИТОРСКОЕ ЗАКЛЮЧЕНИЕ", {"bold": True, "size": 20})], font=font)
    draft = _add_styled_paragraph(doc, font=font, align="center", space_after=18)
    _add_runs(draft, [("(черновик)", {"italic": True, "size": 14})], font=font)
    name = _add_styled_paragraph(doc, font=font, align="center", space_after=12)
    _add_runs(
        name,
        [((inspection_name or "Проверка").strip(), {"bold": True, "size": 16})],
        font=font,
    )
    period_s = (period or "").strip() or "уточняется"
    per = _add_styled_paragraph(doc, font=font, align="center", space_after=8)
    _add_runs(per, [(f"Аудируемый период: {period_s}", {"italic": True, "size": 12})], font=font)
    for _ in range(8):
        _blank(doc, font=font, after=0)
    note = _add_styled_paragraph(doc, font=font, align="center", space_after=0)
    _add_runs(
        note,
        [
            (
                "Черновик для правки аудитором. Не утверждённый акт службы внутреннего аудита.",
                {"italic": True, "size": 11},
            )
        ],
        font=font,
    )
    doc.add_page_break()


def _write_toc(doc: Document, entries: list[tuple[str, str]], *, font: str) -> None:
    h = _add_styled_paragraph(doc, font=font, align="left", space_after=12, first_line=False)
    _add_runs(h, [(_TOC_HEADING, {"bold": True, "size": 16})], font=font)
    for roman, title in entries:
        p = _add_styled_paragraph(doc, font=font, align="justify", space_after=8, first_line=False)
        _add_runs(
            p,
            [
                (f"{roman}.", {"bold": True}),
                ("\t", {}),
                (title if title.endswith(".") else title + ".", {}),
            ],
            font=font,
        )
        _blank(doc, font=font, after=0)
    doc.add_page_break()


def _write_general_section(doc: Document, section: ReportSection, *, font: str) -> None:
    h = _add_styled_paragraph(doc, font=font, align="left", space_before=12, space_after=12, first_line=False)
    _add_runs(
        h,
        [
            (f"{section.roman}.", {"bold": True, "size": 16}),
            ("\t", {"bold": True, "size": 16}),
            (_SECTION_LAST, {"bold": True, "size": 16}),
        ],
        font=font,
    )
    for label, value in section.general_items:
        lab = _add_styled_paragraph(
            doc, font=font, align="justify", space_before=8, space_after=2, first_line=False
        )
        _add_runs(lab, [(label, {"bold": True})], font=font)
        _add_opinion_paragraph(
            doc,
            value or "уточняется",
            font=font,
            size=_FONT_SIZE,
            first_line=False,
            space_after=8,
        )


def write_conclusion_docx(
    path: Path,
    *,
    inspection_name: str,
    period: str | None,
    case_id: str,
    opinion_body: str,
    report: ConclusionDocument,
    font: str = "Times New Roman",
) -> Path:
    doc = Document()
    _set_document_base_font(doc, font, _FONT_SIZE)
    section = doc.sections[0]
    section.page_width = Cm(21.0)
    section.page_height = Cm(29.7)
    section.left_margin = Cm(3.0)
    section.right_margin = Cm(1.5)
    section.top_margin = Cm(2.0)
    section.bottom_margin = Cm(2.0)

    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    fr = footer.add_run(f"Черновик · {_TITLE} · {inspection_name} · кейс {case_id}")
    _set_run_font(fr, size=9, italic=True, font=font)

    _write_title_page(doc, inspection_name=inspection_name, period=period, font=font)
    _write_toc(doc, toc_entries(report), font=font)

    h1 = _add_styled_paragraph(doc, font=font, align="left", space_after=10, first_line=False)
    _add_runs(
        h1,
        [
            ("I.", {"bold": True, "size": 16}),
            ("\t", {"bold": True, "size": 16}),
            (_SECTION_I, {"bold": True, "size": 16}),
        ],
        font=font,
    )
    add_opinion_markdown(doc, opinion_body or "", font=font)
    doc.add_page_break()

    for block in report.sections:
        if block.kind == "general":
            _write_general_section(doc, block, font=font)
            continue
        _add_section_heading(doc, block.roman, font=font, title=block.title)
        if block.intro:
            _add_body_markdown(doc, block.intro, font=font)
        for observation in block.observations:
            if observation.body:
                _add_body_markdown(doc, observation.body, font=font)
            _add_observation_box(doc, observation, font=font)

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path
