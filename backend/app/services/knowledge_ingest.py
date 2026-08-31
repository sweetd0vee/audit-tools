from __future__ import annotations

from pathlib import Path

from app.filenames import safe_stem
from app.models import CaseState, KnowledgeItem
from app.services.chunker import normalize_npa_text
from app.services.extract import TEXT_EXTS, extract_text
from app.storage import store


def text_dir(case_id: str) -> Path:
    path = store.case_dir(case_id) / "knowledge_text"
    path.mkdir(parents=True, exist_ok=True)
    return path


def summaries_dir(case_id: str) -> Path:
    path = store.case_dir(case_id) / "summaries"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _is_duplicate_html(path: Path) -> bool:
    if path.suffix.lower() not in {".html", ".htm"}:
        return False
    return path.with_suffix(".txt").exists()


def _title_for_file(path: Path, state: CaseState) -> str:
    for doc in state.documents:
        if doc.local_path and Path(doc.local_path).name == path.name:
            return doc.title
        txt = Path(doc.local_path).with_suffix(".txt").name if doc.local_path else ""
        if txt and txt == path.name:
            return doc.title
    return path.stem.replace("_", " ")


def _origin_id(path: Path, state: CaseState) -> str | None:
    for doc in state.documents:
        if not doc.local_path:
            continue
        local = Path(doc.local_path)
        if local.name == path.name:
            return doc.id
        if local.with_suffix(".txt").name == path.name:
            return doc.id
    return None


def _wanted_library_names(state: CaseState) -> set[str] | None:
    """Filenames that belong to the current selection (plus auditor uploads).

    None means 'ingest everything' — no successful downloads recorded yet,
    so we cannot tell leftovers from the first copy.
    """
    names: set[str] = set()
    has_download = False
    for doc in state.documents:
        if not (doc.selected and doc.download_status == "ok" and doc.local_path):
            continue
        has_download = True
        name = Path(doc.local_path).name
        names.add(name)
        names.add(Path(name).with_suffix(".txt").name)
    for item in state.knowledge:
        if item.source == "uploaded" and item.filename:
            names.add(item.filename)
    if not has_download and not names:
        return None
    return names


def item_text(item: KnowledgeItem) -> str:
    if item.text_path and Path(item.text_path).exists():
        return normalize_npa_text(Path(item.text_path).read_text(encoding="utf-8"))
    if item.local_path and Path(item.local_path).exists():
        return normalize_npa_text(extract_text(Path(item.local_path)))
    return ""


def ingest_library(case_id: str) -> CaseState:
    """Register files from knowledge_raw into knowledge items + extract text."""
    state = store.get(case_id)
    lib = store.library_dir(case_id)
    wanted = _wanted_library_names(state)
    items = [
        item
        for item in state.knowledge
        if item.source == "uploaded"
        or wanted is None
        or item.filename in wanted
        or (item.origin_document_id and any(
            d.id == item.origin_document_id and d.selected and d.download_status == "ok"
            for d in state.documents
        ))
    ]
    existing = {item.filename: item for item in items}
    extracted_dir = text_dir(case_id)

    for path in sorted(lib.iterdir()):
        if not path.is_file() or path.suffix.lower() not in TEXT_EXTS:
            continue
        if wanted is not None and path.name not in wanted:
            continue
        if _is_duplicate_html(path):
            continue
        if path.name in existing:
            continue
        try:
            text = normalize_npa_text(extract_text(path))
        except Exception as exc:  # noqa: BLE001
            items.append(
                KnowledgeItem(
                    title=_title_for_file(path, state),
                    source="downloaded",
                    filename=path.name,
                    local_path=str(path),
                    origin_document_id=_origin_id(path, state),
                    bytes=path.stat().st_size,
                    extract_status="failed",
                    extract_error=str(exc),
                )
            )
            continue

        text_path = extracted_dir / f"{safe_stem(path.name)}.txt"
        text_path.write_text(text, encoding="utf-8")
        items.append(
            KnowledgeItem(
                title=_title_for_file(path, state),
                source="downloaded",
                filename=path.name,
                local_path=str(path),
                text_path=str(text_path),
                origin_document_id=_origin_id(path, state),
                bytes=path.stat().st_size,
                extract_status="ok",
                char_count=len(text),
            )
        )

    state.knowledge = items
    store.save(state)
    return state


def add_uploaded_file(case_id: str, filename: str, content: bytes) -> KnowledgeItem:
    state = store.get(case_id)
    lib = store.library_dir(case_id)
    safe = f"U_{len(state.knowledge)+1:02d}_{safe_stem(filename)}{Path(filename).suffix.lower() or '.bin'}"
    dest = lib / safe
    dest.write_bytes(content)

    item = KnowledgeItem(
        title=Path(filename).stem.replace("_", " "),
        source="uploaded",
        filename=dest.name,
        local_path=str(dest),
        bytes=len(content),
        extract_status="pending",
    )
    try:
        text = normalize_npa_text(extract_text(dest))
        dest_text = text_dir(case_id) / f"{Path(safe).stem}.txt"
        dest_text.write_text(text, encoding="utf-8")
        item.text_path = str(dest_text)
        item.extract_status = "ok"
        item.char_count = len(text)
    except Exception as exc:  # noqa: BLE001
        item.extract_status = "failed"
        item.extract_error = str(exc)

    state.knowledge.append(item)
    store.write_library_archive(case_id)
    store.save(state)
    return item
