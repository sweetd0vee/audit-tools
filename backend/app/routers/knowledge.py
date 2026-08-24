from __future__ import annotations

import io
import json
import logging
import traceback
import zipfile
from collections.abc import AsyncIterator

from typing import Optional

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from app.models import AskRequest, AskResponse, BriefRequest, OpenWebUISyncRequest
from app.services.brief_flow import (
    brief_download_name,
    brief_status,
    build_brief,
    build_brief_events,
    resolve_brief_file,
)
from app.services.knowledge_flow import (
    add_uploaded_file,
    ask,
    build_knowledge_events,
    export_pack_files,
    ingest_library,
    openwebui_status,
    rebuild_index,
    sync_openwebui,
)
from app.services.openwebui_client import OpenWebUIError
from app.storage import store

router = APIRouter(prefix="/api/v1", tags=["knowledge"])
logger = logging.getLogger(__name__)


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.get("/cases/{case_id}/knowledge")
def get_knowledge(case_id: str):
    try:
        state = ingest_library(case_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "case_id": case_id,
        "status": state.status,
        "inspection_name": state.inspection_name,
        "keywords": state.keywords,
        "openwebui_knowledge_id": state.meta.get("openwebui_knowledge_id"),
        "openwebui_knowledge_name": state.meta.get("openwebui_knowledge_name"),
        "items": [k.model_dump() for k in state.knowledge],
    }


@router.post("/cases/{case_id}/knowledge/upload")
async def upload_knowledge(case_id: str, files: list[UploadFile] = File(...)):
    try:
        store.get(case_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    added = []
    errors = []
    for f in files:
        raw = await f.read()
        if not raw:
            errors.append({"filename": f.filename, "error": "empty file"})
            continue
        try:
            item = add_uploaded_file(case_id, f.filename or "document.bin", raw)
            added.append(item.model_dump())
        except Exception as exc:  # noqa: BLE001
            errors.append({"filename": f.filename, "error": str(exc)})

    state = store.get(case_id)
    return {
        "case_id": case_id,
        "added": added,
        "errors": errors,
        "items": [k.model_dump() for k in state.knowledge],
    }


@router.post("/cases/{case_id}/knowledge/ingest")
def ingest(case_id: str):
    try:
        state = ingest_library(case_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"case_id": case_id, "items": [k.model_dump() for k in state.knowledge]}


@router.post("/cases/{case_id}/knowledge/index")
def index_knowledge(case_id: str):
    """Collect chunks from downloaded txt without summaries or Open WebUI."""
    try:
        store.get(case_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    payload = rebuild_index(case_id)
    state = store.get(case_id)
    return {
        "case_id": case_id,
        "chunks": len(payload.get("chunks") or []),
        "items": [k.model_dump() for k in state.knowledge],
    }


@router.get("/cases/{case_id}/knowledge/build/stream")
async def build_stream(case_id: str):
    try:
        store.get(case_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    async def gen() -> AsyncIterator[str]:
        try:
            async for event in build_knowledge_events(case_id):
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


@router.post("/cases/{case_id}/knowledge/ask", response_model=AskResponse)
async def ask_knowledge(case_id: str, body: AskRequest) -> AskResponse:
    try:
        store.get(case_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        result = await ask(case_id, body.question, body.top_k)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Ask failed: {exc}") from exc
    return AskResponse(**result)


@router.get("/cases/{case_id}/knowledge/brief")
def get_brief(case_id: str):
    try:
        store.get(case_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return brief_status(case_id)


@router.post("/cases/{case_id}/knowledge/brief")
async def post_brief(case_id: str, body: Optional[BriefRequest] = None):
    try:
        store.get(case_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    force = bool(body and body.force)
    try:
        return await build_brief(case_id, force=force)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Brief failed: {exc}") from exc


@router.get("/cases/{case_id}/knowledge/brief/stream")
async def brief_stream(case_id: str, force: bool = Query(default=False)):
    try:
        store.get(case_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    async def gen() -> AsyncIterator[str]:
        try:
            async for event in build_brief_events(case_id, force=force):
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


@router.get("/cases/{case_id}/knowledge/brief.docx")
def download_brief_docx(case_id: str):
    try:
        state = store.get(case_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    path = resolve_brief_file(case_id, "docx")
    if not path:
        raise HTTPException(
            status_code=404,
            detail="Обзора ещё нет. Напишите в чате «саммари».",
        )
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=brief_download_name(state.inspection_name, case_id, "docx"),
    )


@router.get("/cases/{case_id}/knowledge/brief.md")
def download_brief_md(case_id: str):
    try:
        state = store.get(case_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    path = resolve_brief_file(case_id, "md")
    if not path:
        raise HTTPException(status_code=404, detail="Markdown саммари ещё нет.")
    return FileResponse(
        path,
        media_type="text/markdown; charset=utf-8",
        filename=brief_download_name(state.inspection_name, case_id, "md"),
    )


@router.get("/cases/{case_id}/knowledge/export")
def export_knowledge(case_id: str):
    try:
        store.get(case_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    files = export_pack_files(case_id)
    if not files:
        raise HTTPException(status_code=400, detail="Нет файлов для экспорта. Соберите базу знаний.")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in files:
            zf.writestr(name, data)
    buf.seek(0)
    filename = f"kb_{case_id}.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/knowledge/openwebui/status")
async def owui_status():
    return await openwebui_status()


@router.post("/cases/{case_id}/knowledge/openwebui/sync")
async def owui_sync(case_id: str, body: Optional[OpenWebUISyncRequest] = None):
    try:
        store.get(case_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    key = (body.api_key if body else None) or None
    try:
        return await sync_openwebui(case_id, key)
    except OpenWebUIError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as extra:  # noqa: BLE001
        loc = ""
        tb = traceback.extract_tb(extra.__traceback__)
        if tb:
            frame = tb[-1]
            loc = f" ({frame.name}:{frame.lineno})"
        logger.exception("Open WebUI sync failed")
        raise HTTPException(
            status_code=502, detail=f"Open WebUI sync failed: {extra}{loc}"
        ) from extra
