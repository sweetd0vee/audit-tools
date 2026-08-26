from __future__ import annotations

import io
import logging
import traceback
import zipfile
from typing import Optional

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from app.http import require_case, sse_response
from app.config import settings
from app.models import (
    AskRequest,
    AskResponse,
    BriefRequest,
    ChatRequest,
    ChatResponse,
    OpenWebUISyncRequest,
)
from app.services.brief_flow import (
    brief_download_name,
    brief_status,
    build_brief,
    build_brief_events,
    resolve_brief_file,
)
from app.services.hypotheses_flow import (
    build_hypotheses,
    build_hypotheses_events,
    hypotheses_download_name,
    hypotheses_status,
    resolve_hypotheses_file,
)
from app.services.program_flow import (
    build_program,
    build_program_events,
    program_download_name,
    program_status,
    resolve_program_file,
)
from app.services.total_flow import (
    build_total,
    build_total_events,
    resolve_total_file,
    total_download_name,
    total_status,
)
from app.services.knowledge_ask import ask
from app.services.knowledge_flow import build_knowledge_events
from app.services.knowledge_index import rebuild_index
from app.services.knowledge_ingest import add_uploaded_file, ingest_library
from app.services.knowledge_owui import export_pack_files, openwebui_status, sync_openwebui
from app.services.ollama_client import chat_messages
from app.services.openwebui_client import OpenWebUIError
from app.storage import store
from app.prompts import prompt

router = APIRouter(prefix="/api/v1", tags=["knowledge"])
logger = logging.getLogger(__name__)


async def _build_artifact(case_id: str, body: Optional[BriefRequest], builder, label: str):
    require_case(case_id)
    force = bool(body and body.force)
    try:
        return await builder(case_id, force=force)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"{label} failed: {exc}") from exc


def _download_artifact(
    case_id: str,
    *,
    kind: str,
    resolver,
    filename_builder,
    not_found: str,
):
    state = require_case(case_id)
    path = resolver(case_id, kind)
    if not path:
        raise HTTPException(status_code=404, detail=not_found)
    if kind == "docx":
        media_type = (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
    elif kind == "xlsx":
        media_type = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    else:
        media_type = "text/markdown; charset=utf-8"
    return FileResponse(
        path,
        media_type=media_type,
        filename=filename_builder(state.inspection_name, case_id, kind),
    )


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
    require_case(case_id)
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
    require_case(case_id)
    payload = rebuild_index(case_id)
    state = store.get(case_id)
    return {
        "case_id": case_id,
        "chunks": len(payload.get("chunks") or []),
        "items": [k.model_dump() for k in state.knowledge],
    }


@router.get("/cases/{case_id}/knowledge/build/stream")
async def build_stream(case_id: str):
    require_case(case_id)
    return sse_response(build_knowledge_events(case_id))


@router.post("/cases/{case_id}/knowledge/ask", response_model=AskResponse)
async def ask_knowledge(case_id: str, body: AskRequest) -> AskResponse:
    require_case(case_id)
    try:
        result = await ask(case_id, body.question, body.top_k)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Ask failed: {exc}") from exc
    return AskResponse(**result)


@router.post("/chat", response_model=ChatResponse)
async def free_chat(body: ChatRequest) -> ChatResponse:
    """Обычный диалог с LLM без RAG по базе знаний кейса."""
    cleaned: list[dict[str, str]] = []
    for msg in body.messages:
        role = (msg.role or "").strip().lower()
        content = (msg.content or "").strip()
        if role not in {"system", "user", "assistant"} or not content:
            continue
        cleaned.append({"role": role, "content": content})
    if not cleaned:
        raise HTTPException(status_code=400, detail="Нужно хотя бы одно сообщение")
    if cleaned[0]["role"] != "system":
        cleaned.insert(0, {"role": "system", "content": (body.system or prompt("chat_system")).strip()})
    try:
        answer = await chat_messages(cleaned, timeout=settings.ollama_timeout_sec)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Chat failed: {exc}") from exc
    return ChatResponse(answer=answer, model=settings.ollama_model)


@router.get("/cases/{case_id}/knowledge/brief")
def get_brief(case_id: str):
    require_case(case_id)
    return brief_status(case_id)


@router.post("/cases/{case_id}/knowledge/brief")
async def post_brief(case_id: str, body: Optional[BriefRequest] = None):
    return await _build_artifact(case_id, body, build_brief, "Brief")


@router.get("/cases/{case_id}/knowledge/brief/stream")
async def brief_stream(case_id: str, force: bool = Query(default=False)):
    require_case(case_id)
    return sse_response(build_brief_events(case_id, force=force))


@router.get("/cases/{case_id}/knowledge/summary.docx")
@router.get("/cases/{case_id}/knowledge/brief.docx")
def download_brief_docx(case_id: str):
    return _download_artifact(
        case_id,
        kind="docx",
        resolver=resolve_brief_file,
        filename_builder=brief_download_name,
        not_found="Обзора ещё нет. Напишите в чате «саммари».",
    )


@router.get("/cases/{case_id}/knowledge/summary.md")
@router.get("/cases/{case_id}/knowledge/brief.md")
def download_brief_md(case_id: str):
    return _download_artifact(
        case_id,
        kind="md",
        resolver=resolve_brief_file,
        filename_builder=brief_download_name,
        not_found="Markdown саммари ещё нет.",
    )


@router.get("/cases/{case_id}/knowledge/total")
def get_total(case_id: str):
    require_case(case_id)
    return total_status(case_id)


@router.post("/cases/{case_id}/knowledge/total")
async def post_total(case_id: str, body: Optional[BriefRequest] = None):
    return await _build_artifact(case_id, body, build_total, "Total")


@router.get("/cases/{case_id}/knowledge/total/stream")
async def total_stream(case_id: str, force: bool = Query(default=False)):
    require_case(case_id)
    return sse_response(build_total_events(case_id, force=force))


@router.get("/cases/{case_id}/knowledge/total.docx")
def download_total_docx(case_id: str):
    return _download_artifact(
        case_id,
        kind="docx",
        resolver=resolve_total_file,
        filename_builder=total_download_name,
        not_found="Саммари total ещё нет. Напишите в чате «саммари total» или «конспект модели».",
    )


@router.get("/cases/{case_id}/knowledge/total.md")
def download_total_md(case_id: str):
    return _download_artifact(
        case_id,
        kind="md",
        resolver=resolve_total_file,
        filename_builder=total_download_name,
        not_found="Markdown саммари total ещё нет.",
    )


@router.get("/cases/{case_id}/knowledge/program")
def get_program(case_id: str):
    require_case(case_id)
    return program_status(case_id)


@router.post("/cases/{case_id}/knowledge/program")
async def post_program(case_id: str, body: Optional[BriefRequest] = None):
    require_case(case_id)
    force = bool(body and body.force)
    try:
        return await build_program(
            case_id,
            force=force,
            items_min=body.items_min if body else None,
            items_max=body.items_max if body else None,
            items=body.items if body else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Program failed: {exc}") from exc


@router.get("/cases/{case_id}/knowledge/program/stream")
async def program_stream(
    case_id: str,
    force: bool = Query(default=False),
    items: Optional[str] = Query(default=None),
    items_min: Optional[int] = Query(default=None, ge=3, le=20),
    items_max: Optional[int] = Query(default=None, ge=3, le=20),
):
    require_case(case_id)
    return sse_response(
        build_program_events(
            case_id,
            force=force,
            items_min=items_min,
            items_max=items_max,
            items=items,
        )
    )


@router.get("/cases/{case_id}/knowledge/program.docx")
def download_program_docx(case_id: str):
    return _download_artifact(
        case_id,
        kind="docx",
        resolver=resolve_program_file,
        filename_builder=program_download_name,
        not_found="Программы проверки ещё нет. Напишите в чате «программа проверки».",
    )


@router.get("/cases/{case_id}/knowledge/program.md")
def download_program_md(case_id: str):
    return _download_artifact(
        case_id,
        kind="md",
        resolver=resolve_program_file,
        filename_builder=program_download_name,
        not_found="Markdown программы проверки ещё нет.",
    )


@router.get("/cases/{case_id}/knowledge/hypotheses")
def get_hypotheses(case_id: str):
    require_case(case_id)
    return hypotheses_status(case_id)


@router.post("/cases/{case_id}/knowledge/hypotheses")
async def post_hypotheses(case_id: str, body: Optional[BriefRequest] = None):
    return await _build_artifact(case_id, body, build_hypotheses, "Hypotheses")


@router.get("/cases/{case_id}/knowledge/hypotheses/stream")
async def hypotheses_stream(case_id: str, force: bool = Query(default=False)):
    require_case(case_id)
    return sse_response(build_hypotheses_events(case_id, force=force))


@router.get("/cases/{case_id}/knowledge/hypotheses.xlsx")
def download_hypotheses_xlsx(case_id: str):
    return _download_artifact(
        case_id,
        kind="xlsx",
        resolver=resolve_hypotheses_file,
        filename_builder=hypotheses_download_name,
        not_found="Чеклист гипотез ещё нет. Напишите в чате «гипотезы».",
    )


@router.get("/cases/{case_id}/knowledge/hypotheses.md")
def download_hypotheses_md(case_id: str):
    return _download_artifact(
        case_id,
        kind="md",
        resolver=resolve_hypotheses_file,
        filename_builder=hypotheses_download_name,
        not_found="Markdown гипотез ещё нет.",
    )


@router.get("/cases/{case_id}/knowledge/export")
def export_knowledge(case_id: str):
    require_case(case_id)
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
    require_case(case_id)
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
