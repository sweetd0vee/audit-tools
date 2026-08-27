from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from app.models import CaseState, CaseStatus, ProposedDocument
from app.services.downloader import NEWS_MARKERS, download_url, usable_url
from app.services.extra_titles import (
    expand_extra_titles,
    guess_doc_type,
    is_plausible_npa_title,
    norm_title,
    search_queries_for_title,
)
from app.services.known_sources import lookup_known_url
from app.services.knowledge_flow import rebuild_index
from app.services.npa_search import expand_official_urls, extract_doc_code, find_candidate_urls
from app.services.ollama_client import propose_documents, propose_documents_events
from app.storage import store


def _persist_propose(state: CaseState, result: dict) -> CaseState:
    state.topics = result["topics"]
    state.documents = [
        ProposedDocument(
            title=d["title"],
            doc_type=d["doc_type"],
            why_needed=d["why_needed"],
            search_queries=d["search_queries"],
            priority=d["priority"],
            selected=False,
        )
        for d in result["documents"]
    ]
    state.status = CaseStatus.proposed
    state.meta["propose_model"] = result["model"]
    state.meta["proposed_at"] = datetime.utcnow().isoformat()
    state.meta["propose_raw"] = result.get("raw")
    state.meta["propose_elapsed_ms"] = result.get("elapsed_ms")
    state.meta["propose_system_prompt"] = result.get("system_prompt")
    state.meta["propose_user_prompt"] = result.get("user_prompt")
    store.save(state)
    return state


async def run_propose(case_id: str) -> CaseState:
    state = store.get(case_id)
    result = await propose_documents(
        inspection_name=state.inspection_name,
        keywords=state.keywords,
    )
    return _persist_propose(state, result)


async def run_propose_events(case_id: str):
    """Async generator of SSE-friendly events; persists case on final result."""
    state = store.get(case_id)
    result_payload = None
    async for event in propose_documents_events(
        inspection_name=state.inspection_name,
        keywords=state.keywords,
    ):
        if event.get("type") == "result":
            result_payload = event["payload"]
        yield event

    if not result_payload:
        raise ValueError("No result from model")

    state = _persist_propose(store.get(case_id), result_payload)
    yield {
        "type": "saved",
        "case_id": case_id,
        "status": state.status.value,
        "documents": [d.model_dump() for d in state.documents],
        "raw_topics": state.topics,
        "model": state.meta.get("propose_model"),
        "elapsed_ms": state.meta.get("propose_elapsed_ms"),
    }


def run_select(
    case_id: str,
    document_ids: list[str],
    manual_urls: dict[str, str] | None = None,
    extra_titles: list[str] | None = None,
) -> CaseState:
    state = store.get(case_id)
    if state.status not in (
        CaseStatus.proposed,
        CaseStatus.selected,
        CaseStatus.ready,
        CaseStatus.failed,
        CaseStatus.downloading,
    ):
        raise ValueError(f"Cannot select in status={state.status.value}")

    extras = expand_extra_titles(extra_titles)
    added_ids: list[str] = []
    for title in extras:
        doc = _ensure_extra_document(state, title)
        added_ids.append(doc.id)

    id_set = set(document_ids or [])
    if not id_set and not added_ids:
        raise ValueError("document_ids or extra_titles must not be empty")

    known = {d.id for d in state.documents}
    unknown = id_set - known
    if unknown:
        raise ValueError(f"Unknown document ids: {sorted(unknown)}")

    if id_set:
        selected = id_set | set(added_ids)
    else:
        selected = {d.id for d in state.documents if d.selected} | set(added_ids)

    if not selected:
        raise ValueError("document_ids or extra_titles must not be empty")

    manual_urls = manual_urls or {}
    for doc in state.documents:
        doc.selected = doc.id in selected
        if doc.id not in manual_urls:
            continue
        cleaned = usable_url(str(manual_urls[doc.id]))
        if not cleaned:
            continue
        if doc.found_url != cleaned and doc.download_status == "ok":
            doc.download_status = None
            doc.local_path = None
            doc.download_error = None
        doc.found_url = cleaned

    state.status = CaseStatus.selected
    state.meta["selected_at"] = datetime.utcnow().isoformat()
    if extras:
        state.meta["extra_titles"] = extras
    store.save(state)
    return state


def _ensure_extra_document(state: CaseState, title: str) -> ProposedDocument:
    needle = norm_title(title)
    for doc in state.documents:
        existing = norm_title(doc.title)
        if existing == needle:
            return doc
        if len(needle) >= 16 and (needle in existing or existing in needle):
            return doc

    doc = ProposedDocument(
        title=title.strip(),
        doc_type=guess_doc_type(title),
        why_needed="Добавлен аудитором по названию",
        search_queries=search_queries_for_title(title),
        priority=1,
        selected=True,
    )
    state.documents.append(doc)
    return doc


def download_candidates(doc: ProposedDocument) -> list[tuple[str, str]]:
    """Unique usable URLs: auditor link first, then curated known_sources."""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []

    def add(url: str | None, source: str) -> None:
        variants = expand_official_urls(url)
        if not variants:
            cleaned = usable_url(url)
            variants = [cleaned] if cleaned else []
        for cleaned in variants:
            if not cleaned or cleaned in seen:
                continue
            if any(marker in cleaned.lower() for marker in NEWS_MARKERS):
                continue
            seen.add(cleaned)
            out.append((cleaned, source))

    add(doc.found_url, "manual")
    add(lookup_known_url(doc.title), "known")
    return out


def _on_disk(doc: ProposedDocument, lib_dir: Path) -> bool:
    if not doc.local_path:
        return False
    return (lib_dir / Path(doc.local_path).name).is_file()


def _should_redownload(doc: ProposedDocument) -> bool:
    """Retry a cached file if it is a news stub, chrome link, or a better official URL is known."""
    url = (doc.found_url or "").lower()
    if any(marker in url for marker in NEWS_MARKERS):
        return True
    known = lookup_known_url(doc.title)
    if known:
        return extract_doc_code(known) != extract_doc_code(doc.found_url)
    significant = re.findall(r"[а-яёa-z]{5,}", (doc.title or "").lower())
    if significant and not any(token in url for token in significant):
        return True
    return False


def _record_download(
    doc: ProposedDocument,
    result: dict,
    source: str,
    manifest_items: list[dict],
) -> None:
    doc.local_path = result["local_path"]
    doc.found_url = result.get("url") or doc.found_url
    doc.download_status = "ok"
    doc.download_error = None
    manifest_items.append(
        {
            "document_id": doc.id,
            "title": doc.title,
            "url": result["url"],
            "source": source,
            "local_path": result["local_path"],
            "sha256": result["sha256"],
            "bytes": result["bytes"],
            "text_extract": result.get("text_extract"),
            "downloaded_at": datetime.utcnow().isoformat(),
        }
    )


async def run_download(case_id: str) -> CaseState:
    state = store.get(case_id)
    selected = [d for d in state.documents if d.selected]
    for doc in selected:
        if is_plausible_npa_title(doc.title):
            continue
        doc.selected = False
        doc.download_status = "skipped"
        doc.download_error = "не похоже на название акта"
    selected = [d for d in state.documents if d.selected]
    if not selected:
        raise ValueError("No selected documents. Call /select first.")

    state.status = CaseStatus.downloading
    store.save(state)

    try:
        return await _download_selected(state, case_id, selected)
    except Exception:
        latest = store.get(case_id)
        if latest.status == CaseStatus.downloading:
            latest.status = CaseStatus.failed
            store.save(latest)
        raise


async def _download_selected(
    state: CaseState,
    case_id: str,
    selected: list[ProposedDocument],
) -> CaseState:
    lib_dir = store.library_dir(case_id)
    manifest_items: list[dict] = []

    for i, doc in enumerate(selected, start=1):
        if (
            doc.download_status == "ok"
            and _on_disk(doc, lib_dir)
            and not _should_redownload(doc)
        ):
            manifest_items.append(
                {
                    "document_id": doc.id,
                    "title": doc.title,
                    "url": doc.found_url,
                    "source": "cached",
                    "local_path": doc.local_path,
                    "downloaded_at": datetime.utcnow().isoformat(),
                }
            )
            continue

        doc.download_status = "searching"
        doc.download_error = None
        store.save(state)

        tried: set[str] = set()
        last_error: str | None = None
        saved = False

        async def try_one(url: str, source: str) -> bool:
            nonlocal last_error, saved
            if not url or url in tried:
                return False
            tried.add(url)
            doc.found_url = url
            doc.download_status = "downloading"
            store.save(state)
            try:
                result = await download_url(url, lib_dir, doc.title, i)
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                return False
            _record_download(doc, result, source, manifest_items)
            saved = True
            return True

        for url, source in download_candidates(doc):
            if await try_one(url, source):
                break

        if not saved:
            try:
                search_hits = await find_candidate_urls(
                    doc.search_queries, title=doc.title
                )
            except Exception as exc:  # noqa: BLE001
                search_hits = []
                last_error = last_error or str(exc)
            for url, source in search_hits:
                if await try_one(url, source):
                    break

        if not saved:
            if last_error:
                doc.download_status = "failed"
                doc.download_error = last_error
            else:
                doc.download_status = "not_found"
                doc.download_error = (
                    "Официальный URL не найден. "
                    "Напишите полную ссылку: к N url https://pravo.by/document/?guid=…"
                )

        store.save(state)

    for doc in state.documents:
        if not doc.selected and not doc.download_status:
            doc.download_status = "skipped"

    ok = sum(1 for d in selected if d.download_status == "ok")
    state.status = CaseStatus.ready if ok > 0 else CaseStatus.failed
    state.meta["downloaded_at"] = datetime.utcnow().isoformat()
    state.meta["library_dir"] = str(lib_dir)

    store.write_manifest(
        case_id,
        {
            "case_id": case_id,
            "inspection_name": state.inspection_name,
            "items": manifest_items,
            "downloaded_ok": ok,
            "downloaded_failed": len(selected) - ok,
        },
    )
    archive = store.write_library_archive(case_id)
    if archive:
        state.meta["archive_path"] = str(archive)
        state.meta["archive_name"] = store.archive_filename(state.inspection_name, case_id)
    store.save(state)
    rebuild_index(case_id)
    return store.get(case_id)
