from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.models import CaseState, CaseStatus, ProposedDocument
from app.services.downloader import download_url, usable_url
from app.services.known_sources import lookup_known_url
from app.services.knowledge_flow import rebuild_index
from app.services.ollama_client import propose_documents, propose_documents_events
from app.services.searxng_client import find_best_url
from app.storage import store


async def run_propose(case_id: str) -> CaseState:
    state = store.get(case_id)
    result = await propose_documents(
        inspection_name=state.inspection_name,
        keywords=state.keywords,
        period=state.period,
    )

    docs = [
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

    state.topics = result["topics"]
    state.documents = docs
    state.status = CaseStatus.proposed
    state.meta["propose_model"] = result["model"]
    state.meta["proposed_at"] = datetime.utcnow().isoformat()
    state.meta["propose_raw"] = result.get("raw")
    state.meta["propose_elapsed_ms"] = result.get("elapsed_ms")
    state.meta["propose_system_prompt"] = result.get("system_prompt")
    state.meta["propose_user_prompt"] = result.get("user_prompt")
    store.save(state)
    return state


async def run_propose_events(case_id: str):
    """Async generator of SSE-friendly events; persists case on final result."""
    state = store.get(case_id)
    result_payload = None
    async for event in propose_documents_events(
        inspection_name=state.inspection_name,
        keywords=state.keywords,
        period=state.period,
    ):
        if event.get("type") == "result":
            result_payload = event["payload"]
        yield event

    if not result_payload:
        raise ValueError("No result from model")

    docs = [
        ProposedDocument(
            title=d["title"],
            doc_type=d["doc_type"],
            why_needed=d["why_needed"],
            search_queries=d["search_queries"],
            priority=d["priority"],
            selected=False,
        )
        for d in result_payload["documents"]
    ]
    state = store.get(case_id)
    state.topics = result_payload["topics"]
    state.documents = docs
    state.status = CaseStatus.proposed
    state.meta["propose_model"] = result_payload["model"]
    state.meta["proposed_at"] = datetime.utcnow().isoformat()
    state.meta["propose_raw"] = result_payload.get("raw")
    state.meta["propose_elapsed_ms"] = result_payload.get("elapsed_ms")
    state.meta["propose_system_prompt"] = result_payload.get("system_prompt")
    state.meta["propose_user_prompt"] = result_payload.get("user_prompt")
    store.save(state)

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
) -> CaseState:
    state = store.get(case_id)
    if state.status not in (
        CaseStatus.proposed,
        CaseStatus.selected,
        CaseStatus.ready,
        CaseStatus.failed,
    ):
        raise ValueError(f"Cannot select in status={state.status}")

    id_set = set(document_ids)
    if not id_set:
        raise ValueError("document_ids must not be empty")

    known = {d.id for d in state.documents}
    unknown = id_set - known
    if unknown:
        raise ValueError(f"Unknown document ids: {sorted(unknown)}")

    manual_urls = manual_urls or {}
    for doc in state.documents:
        doc.selected = doc.id in id_set
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
    store.save(state)
    return state


def download_candidates(doc: ProposedDocument) -> list[tuple[str, str]]:
    """Unique usable URLs: auditor link first, then curated known_sources."""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []

    def add(url: str | None, source: str) -> None:
        cleaned = usable_url(url)
        if not cleaned or cleaned in seen:
            return
        seen.add(cleaned)
        out.append((cleaned, source))

    add(doc.found_url, "manual")
    add(lookup_known_url(doc.title), "known")
    return out


def _on_disk(doc: ProposedDocument, lib_dir: Path) -> bool:
    if not doc.local_path:
        return False
    return (lib_dir / Path(doc.local_path).name).is_file()


async def run_download(case_id: str) -> CaseState:
    state = store.get(case_id)
    selected = [d for d in state.documents if d.selected]
    if not selected:
        raise ValueError("No selected documents. Call /select first.")

    state.status = CaseStatus.downloading
    store.save(state)

    lib_dir = store.library_dir(case_id)
    manifest_items = []

    for i, doc in enumerate(selected, start=1):
        if doc.download_status == "ok" and _on_disk(doc, lib_dir):
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

        for url, source in download_candidates(doc):
            tried.add(url)
            doc.found_url = url
            doc.download_status = "downloading"
            store.save(state)
            try:
                result = await download_url(url, lib_dir, doc.title, i)
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                continue
            doc.local_path = result["local_path"]
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
            saved = True
            break

        if not saved:
            try:
                hit = await find_best_url(doc.search_queries, title=doc.title)
            except Exception as exc:  # noqa: BLE001
                hit = None
                last_error = last_error or str(exc)
            url = usable_url((hit or {}).get("url")) if hit else None
            if url and url not in tried:
                doc.found_url = url
                doc.download_status = "downloading"
                store.save(state)
                try:
                    result = await download_url(url, lib_dir, doc.title, i)
                    doc.local_path = result["local_path"]
                    doc.download_status = "ok"
                    doc.download_error = None
                    manifest_items.append(
                        {
                            "document_id": doc.id,
                            "title": doc.title,
                            "url": result["url"],
                            "source": "searxng",
                            "local_path": result["local_path"],
                            "sha256": result["sha256"],
                            "bytes": result["bytes"],
                            "text_extract": result.get("text_extract"),
                            "downloaded_at": datetime.utcnow().isoformat(),
                        }
                    )
                    saved = True
                except Exception as exc:  # noqa: BLE001
                    last_error = str(exc)

        if not saved:
            if last_error:
                doc.download_status = "failed"
                doc.download_error = last_error
            else:
                doc.download_status = "not_found"
                doc.download_error = (
                    "URL not found (SearXNG empty/suspended). "
                    "Pass a full official URL or extend known_sources."
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
    # Тексты + чанки сразу — ask в чате не ждёт отдельный /knowledge/build
    rebuild_index(case_id)
    return store.get(case_id)
