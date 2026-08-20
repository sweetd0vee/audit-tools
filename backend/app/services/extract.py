from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup

TEXT_EXTS = {".txt", ".md", ".html", ".htm", ".pdf", ".docx", ".rtf"}


def extract_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in {".txt", ".md"}:
        return _read_text_file(path)
    if ext in {".html", ".htm"}:
        return _html_to_text(path.read_bytes())
    if ext == ".pdf":
        return _pdf_to_text(path)
    if ext == ".docx":
        return _docx_to_text(path)
    if ext == ".rtf":
        return _read_text_file(path)
    raise ValueError(f"Unsupported file type: {ext}")


def _read_text_file(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8", "utf-8-sig", "cp1251", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _html_to_text(content: bytes) -> str:
    soup = BeautifulSoup(content, "lxml")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    return _clean_ws(text)


def _pdf_to_text(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    parts = []
    for page in reader.pages:
        parts.append(page.extract_text() or "")
    return _clean_ws("\n".join(parts))


def _docx_to_text(path: Path) -> str:
    from docx import Document

    doc = Document(str(path))
    return _clean_ws("\n".join(p.text for p in doc.paragraphs if p.text.strip()))


def _clean_ws(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines()]
    out: list[str] = []
    blank = 0
    for ln in lines:
        if not ln:
            blank += 1
            if blank <= 1:
                out.append("")
            continue
        blank = 0
        out.append(ln)
    return "\n".join(out).strip()
