from __future__ import annotations

import io
import json
import logging
import re
import traceback
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import ValidationError

from app.config import settings
from app.http import locked_events, require_case, sse_response
from app.models import (
    AskRequest,
    AskResponse,
    BriefRequest,
    ChatRequest,
    ChatResponse,
    OpenWebUISyncRequest,
    SelectHypothesesRequest,
)
from app.prompts import prompt
from app.services.brief_flow import (
    brief_download_name,
    brief_status,
    build_brief,
    build_brief_events,
    resolve_brief_file,
)
from app.services.conclusion_flow import (
    build_conclusion,
    build_conclusion_events,
    conclusion_download_name,
    conclusion_status,
    refresh_conclusion_docx,
    resolve_conclusion_file,
)
from app.services.extract import TEXT_EXTS
from app.services.hypotheses_flow import (
    build_hypotheses,
    build_hypotheses_events,
    hypotheses_download_name,
    hypotheses_status,
    resolve_hypotheses_file,
    select_hypotheses,
)
from app.services.knowledge_ask import ask
from app.services.knowledge_flow import build_knowledge_events
from app.services.knowledge_index import rebuild_index
from app.services.knowledge_ingest import add_uploaded_file, ingest_library
from app.services.knowledge_owui import export_pack_files, openwebui_status, sync_openwebui
from app.services.ollama_client import chat_messages
from app.services.openwebui_client import OpenWebUIError
from app.services.opinion_flow import (
    build_opinion,
    build_opinion_events,
    opinion_download_name,
    opinion_status,
    resolve_opinion_file,
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
from app.storage import async_lock, store

router = APIRouter(prefix="/api/v1", tags=["knowledge"])
logger = logging.getLogger(__name__)

_MEDIA_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


async def _build_artifact(
    case_id: str,
    body: Optional[BriefRequest],
    builder,
    label: str,
    **kwargs,
):
    require_case(case_id)
    force = bool(body and body.force)
    try:
        async with async_lock(case_id):
            return await builder(case_id, force=force, **kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("%s failed case=%s", label, case_id)
        raise HTTPException(status_code=502, detail=f"{label} failed: {exc}") from exc


def _stream_artifact(case_id: str, events):
    require_case(case_id)
    return sse_response(locked_events(case_id, events))


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
    media_type = _MEDIA_TYPES.get(kind, "text/markdown; charset=utf-8")
    return FileResponse(
        path,
        media_type=media_type,
        filename=filename_builder(state.inspection_name, case_id, kind),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


@router.get("/cases/{case_id}/knowledge")
def get_knowledge(case_id: str):
    state = require_case(case_id)
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
    async with async_lock(case_id):
        for f in files:
            raw = await f.read()
            if not raw:
                errors.append({"filename": f.filename, "error": "empty file"})
                continue
            if len(raw) > settings.max_upload_bytes:
                errors.append(
                    {
                        "filename": f.filename,
                        "error": f"файл больше {settings.max_upload_bytes} байт",
                    }
                )
                continue
            suffix = Path(f.filename or "document.bin").suffix.lower() or ".bin"
            if suffix not in TEXT_EXTS:
                errors.append(
                    {
                        "filename": f.filename,
                        "error": f"Неподдерживаемый тип файла: {suffix}",
                    }
                )
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
async def ingest(case_id: str):
    require_case(case_id)
    try:
        async with async_lock(case_id):
            state = ingest_library(case_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"case_id": case_id, "items": [k.model_dump() for k in state.knowledge]}


@router.post("/cases/{case_id}/knowledge/index")
async def index_knowledge(case_id: str):
    """Collect chunks from downloaded txt without summaries or Open WebUI."""
    require_case(case_id)
    async with async_lock(case_id):
        payload = rebuild_index(case_id)
        state = store.get(case_id)
    return {
        "case_id": case_id,
        "chunks": len(payload.get("chunks") or []),
        "items": [k.model_dump() for k in state.knowledge],
    }


@router.get("/cases/{case_id}/knowledge/build/stream")
async def build_stream(case_id: str):
    return _stream_artifact(case_id, build_knowledge_events(case_id))


@router.post("/cases/{case_id}/knowledge/ask", response_model=AskResponse)
async def ask_knowledge(case_id: str, body: AskRequest) -> AskResponse:
    require_case(case_id)
    try:
        async with async_lock(case_id):
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
    return _stream_artifact(case_id, build_brief_events(case_id, force=force))


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
    return _stream_artifact(case_id, build_total_events(case_id, force=force))


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
    return await _build_artifact(
        case_id,
        body,
        build_program,
        "Program",
        items_min=body.items_min if body else None,
        items_max=body.items_max if body else None,
        items=body.items if body else None,
    )


@router.get("/cases/{case_id}/knowledge/program/stream")
async def program_stream(
    case_id: str,
    force: bool = Query(default=False),
    items: Optional[str] = Query(default=None),
    items_min: Optional[int] = Query(default=None, ge=3, le=20),
    items_max: Optional[int] = Query(default=None, ge=3, le=20),
):
    return _stream_artifact(
        case_id,
        build_program_events(
            case_id,
            force=force,
            items_min=items_min,
            items_max=items_max,
            items=items,
        ),
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
    return _stream_artifact(case_id, build_hypotheses_events(case_id, force=force))


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


@router.post("/cases/{case_id}/knowledge/hypotheses/select")
async def post_select_hypotheses(case_id: str, request: Request):
    require_case(case_id)
    try:
        body, extra_xlsx, extra_filename = await _parse_select_hypotheses_request(request)
        extra_rows = body.extra_hypotheses or None
        async with async_lock(case_id):
            return select_hypotheses(
                case_id,
                numbers=body.numbers,
                all_high=body.all_high,
                all_rows=body.all_rows,
                keep_numbers=body.keep_numbers,
                extra_xlsx=extra_xlsx,
                extra_filename=extra_filename,
                extra_rows=extra_rows,
            )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _form_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "on", "да"}


def _form_numbers(value: object) -> list[int]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [int(n) for n in value]
    text = str(value).strip()
    if text.startswith("["):
        parsed = json.loads(text)
        if not isinstance(parsed, list):
            return []
        return [int(n) for n in parsed]
    return [int(n) for n in re.findall(r"\d+", text)]


async def _parse_select_hypotheses_request(
    request: Request,
) -> tuple[SelectHypothesesRequest, bytes | None, str | None]:
    content_type = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" in content_type:
        form = await request.form()
        extra = form.get("extra") or form.get("file")
        extra_xlsx: bytes | None = None
        extra_filename: str | None = None
        if extra is not None and hasattr(extra, "read"):
            extra_xlsx = await extra.read()
            extra_filename = getattr(extra, "filename", None) or "auditor.xlsx"
            if extra_xlsx and len(extra_xlsx) > settings.max_upload_bytes:
                raise ValueError(f"файл больше {settings.max_upload_bytes} байт")
            suffix = Path(extra_filename).suffix.lower()
            if suffix not in {".xlsx", ".xlsm"}:
                raise ValueError("Свои гипотезы принимаются только как .xlsx")
        extra_rows_raw = form.get("extra_hypotheses")
        extra_rows: list[dict] = []
        if extra_rows_raw:
            parsed = json.loads(str(extra_rows_raw))
            if isinstance(parsed, list):
                extra_rows = [row for row in parsed if isinstance(row, dict)]
        body = SelectHypothesesRequest(
            numbers=_form_numbers(form.get("numbers")),
            all_high=_form_bool(form.get("all_high")),
            all_rows=_form_bool(form.get("all_rows")),
            keep_numbers=_form_bool(form.get("keep_numbers")),
            extra_hypotheses=extra_rows,
        )
        return body, extra_xlsx, extra_filename
    payload = await request.json()
    return SelectHypothesesRequest.model_validate(payload), None, None


@router.get("/cases/{case_id}/knowledge/opinion")
def get_opinion(case_id: str):
    require_case(case_id)
    return opinion_status(case_id)


@router.post("/cases/{case_id}/knowledge/opinion")
async def post_opinion(case_id: str, body: Optional[BriefRequest] = None):
    return await _build_artifact(
        case_id,
        body,
        build_opinion,
        "Opinion",
        font=body.font if body else None,
    )


@router.get("/cases/{case_id}/knowledge/opinion/stream")
async def opinion_stream(
    case_id: str,
    force: bool = Query(default=False),
    font: Optional[str] = Query(default=None),
):
    return _stream_artifact(
        case_id,
        build_opinion_events(case_id, force=force, font=font),
    )


@router.get("/cases/{case_id}/knowledge/opinion.docx")
def download_opinion_docx(case_id: str):
    return _download_artifact(
        case_id,
        kind="docx",
        resolver=resolve_opinion_file,
        filename_builder=opinion_download_name,
        not_found="Аудиторского мнения ещё нет. Напишите в чате «аудиторское мнение» после `утверждаю гипотезы …`.",
    )


@router.get("/cases/{case_id}/knowledge/opinion.md")
def download_opinion_md(case_id: str):
    return _download_artifact(
        case_id,
        kind="md",
        resolver=resolve_opinion_file,
        filename_builder=opinion_download_name,
        not_found="Markdown аудиторского мнения ещё нет.",
    )


@router.get("/cases/{case_id}/knowledge/conclusion")
def get_conclusion(case_id: str):
    require_case(case_id)
    return conclusion_status(case_id)


@router.post("/cases/{case_id}/knowledge/conclusion")
async def post_conclusion(case_id: str, body: Optional[BriefRequest] = None):
    return await _build_artifact(
        case_id,
        body,
        build_conclusion,
        "Conclusion",
        font=body.font if body else None,
    )


@router.get("/cases/{case_id}/knowledge/conclusion/stream")
async def conclusion_stream(
    case_id: str,
    force: bool = Query(default=False),
    font: Optional[str] = Query(default=None),
):
    return _stream_artifact(
        case_id,
        build_conclusion_events(case_id, force=force, font=font),
    )


@router.get("/cases/{case_id}/knowledge/conclusion.docx")
def download_conclusion_docx(case_id: str):
    refresh_conclusion_docx(case_id)
    return _download_artifact(
        case_id,
        kind="docx",
        resolver=resolve_conclusion_file,
        filename_builder=conclusion_download_name,
        not_found=(
            "Аудиторского заключения ещё нет. Напишите в чате «аудиторское заключение» "
            "после `аудиторское мнение`."
        ),
    )


@router.get("/cases/{case_id}/knowledge/conclusion.md")
def download_conclusion_md(case_id: str):
    return _download_artifact(
        case_id,
        kind="md",
        resolver=resolve_conclusion_file,
        filename_builder=conclusion_download_name,
        not_found="Markdown аудиторского заключения ещё нет.",
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
        async with async_lock(case_id):
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
