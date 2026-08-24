from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from app.models import (
    CaseStatus,
    CaseSummary,
    CreateCaseRequest,
    CreateCaseResponse,
    DownloadResponse,
    ProposeResponse,
    SelectDocumentsRequest,
    SelectDocumentsResponse,
)
from app.services.library_flow import run_download, run_propose, run_propose_events, run_select
from app.storage import store

router = APIRouter(prefix="/api/v1", tags=["library"])


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.post("/cases", response_model=CreateCaseResponse)
def create_case(body: CreateCaseRequest) -> CreateCaseResponse:
    state = store.create(
        inspection_name=body.inspection_name,
        keywords=body.keywords,
        period=body.period,
        notes=body.notes,
    )
    return CreateCaseResponse(
        case_id=state.case_id,
        status=state.status,
        inspection_name=state.inspection_name,
        keywords=state.keywords,
        created_at=state.created_at,
    )


@router.get("/cases", response_model=list[CaseSummary])
def list_cases() -> list[CaseSummary]:
    out = []
    for s in store.list_cases():
        out.append(
            CaseSummary(
                case_id=s.case_id,
                status=s.status,
                inspection_name=s.inspection_name,
                keywords=s.keywords,
                created_at=s.created_at,
                documents_total=len(s.documents),
                documents_selected=sum(1 for d in s.documents if d.selected),
                documents_downloaded=sum(1 for d in s.documents if d.download_status == "ok"),
            )
        )
    return out


@router.get("/cases/{case_id}")
def get_case(case_id: str):
    try:
        return store.get(case_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/cases/{case_id}/propose", response_model=ProposeResponse)
async def propose(case_id: str) -> ProposeResponse:
    try:
        store.get(case_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        state = await run_propose(case_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Propose failed: {exc}") from exc

    return ProposeResponse(
        case_id=state.case_id,
        status=state.status,
        documents=state.documents,
        model=state.meta.get("propose_model", ""),
        raw_topics=state.topics,
        raw_response=state.meta.get("propose_raw"),
        system_prompt=state.meta.get("propose_system_prompt"),
        user_prompt=state.meta.get("propose_user_prompt"),
        elapsed_ms=state.meta.get("propose_elapsed_ms"),
    )


@router.get("/cases/{case_id}/propose/stream")
async def propose_stream(case_id: str):
    """SSE stream: status / chat / token / result / saved / error."""
    try:
        store.get(case_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    async def gen() -> AsyncIterator[str]:
        try:
            async for event in run_propose_events(case_id):
                yield _sse(event)
            yield _sse({"type": "done"})
        except Exception as exc:  # noqa: BLE001
            yield _sse({"type": "error", "message": str(exc)})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/cases/{case_id}/select", response_model=SelectDocumentsResponse)
def select(case_id: str, body: SelectDocumentsRequest) -> SelectDocumentsResponse:
    try:
        state = run_select(case_id, body.document_ids, body.manual_urls)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return SelectDocumentsResponse(
        case_id=state.case_id,
        status=state.status,
        selected_count=sum(1 for d in state.documents if d.selected),
        documents=state.documents,
    )


@router.post("/cases/{case_id}/download", response_model=DownloadResponse)
async def download(case_id: str) -> DownloadResponse:
    try:
        store.get(case_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        state = await run_download(case_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Download failed: {exc}") from exc

    selected = [d for d in state.documents if d.selected]
    ok = sum(1 for d in selected if d.download_status == "ok")
    failed = len(selected) - ok
    archive_name = state.meta.get("archive_name")

    return DownloadResponse(
        case_id=state.case_id,
        status=state.status,
        downloaded=ok,
        failed=failed,
        library_dir=str(store.library_dir(case_id)),
        archive_name=archive_name,
        archive_url=f"/api/v1/cases/{case_id}/library/archive" if archive_name else None,
        documents=state.documents,
    )


@router.get("/cases/{case_id}/library")
def library(case_id: str):
    try:
        state = store.get(case_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    lib = store.library_dir(case_id)
    files = sorted(p.name for p in lib.iterdir() if p.is_file())
    archive_name = state.meta.get("archive_name")
    return {
        "case_id": case_id,
        "status": state.status,
        "inspection_name": state.inspection_name,
        "library_dir": str(lib),
        "archive_name": archive_name,
        "archive_url": f"/api/v1/cases/{case_id}/library/archive" if archive_name else None,
        "files": files,
        "documents": [
            {
                "id": d.id,
                "title": d.title,
                "selected": d.selected,
                "found_url": d.found_url,
                "local_path": d.local_path,
                "download_status": d.download_status,
                "download_error": d.download_error,
            }
            for d in state.documents
        ],
    }


@router.get("/cases/{case_id}/library/archive")
def library_archive(case_id: str):
    try:
        state = store.get(case_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    path = store.archive_path(case_id)
    if not path.exists():
        path = store.write_library_archive(case_id)
        if path and path.exists() and not state.meta.get("archive_name"):
            state.meta["archive_name"] = store.archive_filename(state.inspection_name, case_id)
            state.meta["archive_path"] = str(path)
            store.save(state)
    if not path or not path.exists():
        raise HTTPException(status_code=404, detail="Archive not found")

    filename = store.archive_filename(state.inspection_name, case_id)
    state.meta["archive_name"] = filename
    return FileResponse(
        path,
        media_type="application/zip",
        filename=filename,
    )


@router.get("/health")
def health():
    return {"ok": True, "status": CaseStatus.created}
