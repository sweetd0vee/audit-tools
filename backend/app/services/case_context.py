from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from app.models import CaseState


def document_catalog(state: CaseState) -> str:
    lines = []
    for i, doc in enumerate(state.documents, start=1):
        if not doc.selected and doc.download_status in (None, "skipped"):
            continue
        status = "скачан" if doc.download_status == "ok" else "в списке, не скачан"
        why = doc.why_needed or ""
        lines.append(f"{i}. {doc.title} [{status}]. {why}".strip())
    for item in state.knowledge:
        if item.source == "uploaded":
            lines.append(f"- Приложен файл: {item.title} ({item.filename})")
    return "\n".join(lines) if lines else "Документы ещё не приложены."


def existing_cards(state: CaseState, *, limit: int = 8000) -> str:
    blocks = []
    for item in state.knowledge:
        if item.summary_status == "ok" and (item.summary or "").strip():
            body = item.summary.strip()
            if len(body) > limit:
                body = body[:limit] + "\n…"
            blocks.append(f"# {item.title}\n{body}")
    return "\n\n".join(blocks)


def optional_block(label: str, text: str, empty: str) -> str:
    body = (text or "").strip()
    if body:
        return f"{label}:\n{body}\n"
    return f"{empty}\n"


def append_npa_sources_markdown(
    lines: list[str],
    sources: list[dict],
    *,
    heading_if_empty: bool = False,
) -> None:
    if not sources and not heading_if_empty:
        return
    lines.append("## Источники: статьи и фрагменты")
    for src in sources:
        article = src.get("article") or "фрагмент"
        url = src.get("url") or ""
        url_line = f" {url}" if url else f" файл `{src.get('filename') or ''}`"
        lines.append(f"### [{src['n']}] {src.get('title')} — {article}")
        lines.append(url_line.strip())
        lines.append("")
        lines.append(f"> {src.get('excerpt') or ''}")
        lines.append("")


def format_npa_sources(sources: list[dict], *, limit: int = 40) -> str:
    blocks = []
    for fr in sources[:limit]:
        article = fr.get("article") or "фрагмент без номера статьи"
        url = fr.get("url") or "URL в библиотеке не зафиксирован"
        text = fr.get("text") or fr.get("excerpt") or ""
        blocks.append(f"[{fr['n']}] {article}\nисточник: {url}\n{text}")
    return "\n\n---\n\n".join(blocks) if blocks else "Фрагментов НПА нет."


def read_truncated_md(
    resolver: Callable[[str, str], Path | None],
    case_id: str,
    limit: int = 14000,
) -> str:
    path = resolver(case_id, "md")
    if not path or not path.exists():
        return ""
    text = path.read_text(encoding="utf-8").strip()
    if len(text) > limit:
        return text[:limit] + "\n…"
    return text
