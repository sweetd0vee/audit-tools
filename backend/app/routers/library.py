from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app import __version__
from app.http import locked_events, require_case, sse_response
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
from app.storage import async_lock, store

router = APIRouter(prefix="/api/v1", tags=["library"])
logger = logging.getLogger(__name__)


@router.post("/cases", response_model=CreateCaseResponse)
def create_case(body: CreateCaseRequest) -> CreateCaseResponse:
    state = store.create(
        inspection_name=body.inspection_name,
        keywords=body.keywords,
        notes=body.notes,
    )
    logger.info("case created id=%s name=%s", state.case_id, state.inspection_name)
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
    return require_case(case_id)


@router.post("/cases/{case_id}/propose", response_model=ProposeResponse)
async def propose(case_id: str) -> ProposeResponse:
    require_case(case_id)
    try:
        async with async_lock(case_id):
            state = await run_propose(case_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("propose failed case=%s", case_id)
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
    require_case(case_id)
    return sse_response(locked_events(case_id, run_propose_events(case_id)))


@router.post("/cases/{case_id}/select", response_model=SelectDocumentsResponse)
async def select(case_id: str, body: SelectDocumentsRequest) -> SelectDocumentsResponse:
    async with async_lock(case_id):
        try:
            state = run_select(
                case_id,
                body.document_ids,
                body.manual_urls,
                extra_titles=body.extra_titles,
            )
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
    require_case(case_id)
    async with async_lock(case_id):
        try:
            state = await run_download(case_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("download failed case=%s", case_id)
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
    state = require_case(case_id)
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
    state = require_case(case_id)
    path = store.archive_path(case_id)
    if not path.exists():
        rebuilt = store.write_library_archive(case_id)
        if rebuilt is not None:
            path = rebuilt
            if path.exists() and not state.meta.get("archive_name"):
                state.meta["archive_name"] = store.archive_filename(
                    state.inspection_name, case_id
                )
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
    return {"status": "ok", "version": __version__}
