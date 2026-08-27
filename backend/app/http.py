from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from app.models import CaseState
from app.storage import store

logger = logging.getLogger(__name__)

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def require_case(case_id: str) -> CaseState:
    try:
        return store.get(case_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def sse_line(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


async def sse_from_events(events: AsyncIterator[dict]) -> AsyncIterator[str]:
    try:
        async for event in events:
            yield sse_line(event)
        yield sse_line({"type": "done"})
    except Exception as exc:  # noqa: BLE001
        logger.exception("SSE stream failed")
        yield sse_line({"type": "error", "message": str(exc)})


def sse_response(events: AsyncIterator[dict]) -> StreamingResponse:
    return StreamingResponse(
        sse_from_events(events),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
