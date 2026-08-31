from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from inspect import isasyncgenfunction
from pathlib import Path
from typing import Any

from app.clock import utc_now
from app.config import settings
from app.filenames import safe_stem
from app.models import CaseState
from app.services.citations import pages_estimate
from app.storage import store


@dataclass(frozen=True)
class ArtifactSpec:
    meta_key: str
    directory: str
    file_prefix: str
    md_name: str
    sources_name: str
    download_suffix: str
    docx_endpoint: str
    md_endpoint: str
    docx_glob: str
    primary_ext: str = "docx"


@dataclass(frozen=True)
class ArtifactPaths:
    primary: Path
    md: Path
    sources: Path


class ElapsedTimer:
    def __init__(self) -> None:
        self._started = time.perf_counter()

    def ms(self) -> int:
        return int((time.perf_counter() - self._started) * 1000)


def artifact_dir(case_id: str, spec: ArtifactSpec) -> Path:
    path = store.case_dir(case_id) / spec.directory
    path.mkdir(parents=True, exist_ok=True)
    return path


def artifact_docx_path(case_id: str, inspection_name: str, spec: ArtifactSpec) -> Path:
    stem = safe_stem(inspection_name or "proverka")
    ext = (spec.primary_ext or "docx").lstrip(".")
    return artifact_dir(case_id, spec) / f"{spec.file_prefix}_{stem}_{case_id}.{ext}"


def artifact_md_path(case_id: str, spec: ArtifactSpec) -> Path:
    return artifact_dir(case_id, spec) / spec.md_name


def artifact_sources_path(case_id: str, spec: ArtifactSpec) -> Path:
    return artifact_dir(case_id, spec) / spec.sources_name


def artifact_paths(case_id: str, inspection_name: str, spec: ArtifactSpec) -> ArtifactPaths:
    return ArtifactPaths(
        primary=artifact_docx_path(case_id, inspection_name, spec),
        md=artifact_md_path(case_id, spec),
        sources=artifact_sources_path(case_id, spec),
    )


def knowledge_ok_count(state: CaseState) -> int:
    return sum(1 for item in state.knowledge if item.extract_status == "ok")


def artifact_stale(
    state: CaseState,
    spec: ArtifactSpec,
    *,
    schema: int | None = None,
    check_items: bool = False,
    extra: dict[str, Any] | None = None,
) -> bool:
    meta = state.meta.get(spec.meta_key) or {}
    path = Path(meta["docx_path"]) if meta.get("docx_path") else None
    if not path or not path.exists():
        return True
    if schema is not None and meta.get("schema") != schema:
        return True
    if check_items and meta.get("items") != knowledge_ok_count(state):
        return True
    for key, value in (extra or {}).items():
        if meta.get(key) != value:
            return True
    return False


def artifact_download_name(
    inspection_name: str,
    spec: ArtifactSpec,
    *,
    ext: str = "docx",
) -> str:
    stem = safe_stem(inspection_name or "proverka")
    suffix = (ext or "docx").lstrip(".")
    return f"{stem}_{spec.download_suffix}.{suffix}"


def resolve_artifact_file(case_id: str, spec: ArtifactSpec, kind: str) -> Path | None:
    state = store.get(case_id)
    meta = state.meta.get(spec.meta_key) or {}
    primary_kinds = {"docx", "xlsx", "primary"}
    if kind in primary_kinds:
        for key in ("docx_path", "xlsx_path"):
            stored = meta.get(key)
            if stored and Path(stored).exists():
                return Path(stored)
        candidate = artifact_docx_path(case_id, state.inspection_name, spec)
        if candidate.exists():
            return candidate
        found = sorted(artifact_dir(case_id, spec).glob(spec.docx_glob))
        return found[-1] if found else None
    stored = meta.get("md_path")
    if stored and Path(stored).exists():
        return Path(stored)
    candidate = artifact_md_path(case_id, spec)
    return candidate if candidate.exists() else None


def artifact_status(case_id: str, spec: ArtifactSpec) -> dict[str, Any]:
    state = store.get(case_id)
    meta = dict(state.meta.get(spec.meta_key) or {})
    docx = (
        Path(meta["docx_path"])
        if meta.get("docx_path")
        else artifact_docx_path(case_id, state.inspection_name, spec)
    )
    ready = docx.exists()
    meta.update(
        {
            "case_id": case_id,
            "ready": ready,
            "docx_path": str(docx) if ready else meta.get("docx_path"),
            "download": spec.docx_endpoint.format(case_id=case_id),
            "markdown": spec.md_endpoint.format(case_id=case_id),
            "inspection_name": state.inspection_name,
        }
    )
    return meta


def save_artifact_meta(
    state: CaseState,
    spec: ArtifactSpec,
    *,
    docx: Path,
    md: Path,
    sources: list[dict],
    body: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "built_at": utc_now().isoformat(),
        "docx_path": str(docx),
        "md_path": str(md),
        "citations": len(sources),
        "chars": len(body),
        "pages_estimate": pages_estimate(body, settings.brief_chars_per_page),
        "download": spec.docx_endpoint.format(case_id=state.case_id),
        "markdown": spec.md_endpoint.format(case_id=state.case_id),
        "ready": True,
        "case_id": state.case_id,
        "inspection_name": state.inspection_name,
    }
    if extra:
        meta.update(extra)
    state.meta[spec.meta_key] = meta
    store.save(state)
    return meta


async def event_result(events: AsyncIterator[dict], error_message: str) -> dict:
    result: dict | None = None
    async for event in events:
        if event.get("type") == "result":
            result = event
    if not result:
        raise ValueError(error_message)
    return result


def sse_status(elapsed_ms: int, message: str) -> dict[str, Any]:
    return {"type": "status", "message": message, "elapsed_ms": elapsed_ms}


def sse_result(
    elapsed_ms: int,
    meta: Mapping[str, Any],
    *,
    digest: list[str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": "result", **dict(meta), "elapsed_ms": elapsed_ms}
    if digest is not None:
        payload["digest"] = digest
    return payload


def reuse_artifact_events(
    case_id: str,
    spec: ArtifactSpec,
    *,
    force: bool,
    stale: bool,
    already_message: str,
    elapsed_ms: int,
) -> list[dict[str, Any]] | None:
    if force or stale:
        return None
    meta = dict(artifact_status(case_id, spec))
    meta["reused"] = True
    return [
        sse_status(elapsed_ms, already_message),
        sse_result(elapsed_ms, meta, digest=[]),
    ]


async def complete_llm(
    coro: Awaitable[str],
    *,
    fail: str,
    empty: str | None = None,
) -> str:
    try:
        raw = await coro
    except Exception as exc:
        raise ValueError(f"{fail}: {exc}") from exc
    if empty is not None and not (raw or "").strip():
        raise ValueError(empty)
    return raw


def write_sources_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def case_stale_extra(state: CaseState, **extra: Any) -> dict[str, Any]:
    return {
        "keywords": list(state.keywords),
        "inspection_name": state.inspection_name,
        **extra,
    }


def upstream_built_at(state: CaseState, *keys: str) -> dict[str, Any]:
    return {f"{key}_built_at": (state.meta.get(key) or {}).get("built_at") for key in keys}


@dataclass
class ArtifactOutcome:
    """Files already written; runner persists sources JSON + meta."""

    body: str
    sources: list[dict]
    extra: dict[str, Any] = field(default_factory=dict)
    digest: list[str] = field(default_factory=list)
    sources_file: Any = None


@dataclass(frozen=True)
class ComposeNotice:
    """Extra SSE status from an async-generator `compose` (retry loops, etc.)."""

    message: str


async def run_llm_artifact_events(
    case_id: str,
    spec: ArtifactSpec,
    *,
    force: bool,
    start_message: str,
    already_message: str,
    writing_message: str,
    load_state: Callable[[str], CaseState],
    is_stale: Callable[[CaseState], bool],
    compose: Callable[[CaseState, Any], Awaitable[Any]],
    write: Callable[[CaseState, ArtifactPaths, Any, Any], ArtifactOutcome],
    compose_fail: str,
    compose_message: str | Callable[[CaseState, Any], str],
    empty_error: str | None = None,
    inspect: Callable[[CaseState], None] | None = None,
    prepare_message: str | None = None,
    prepare: Callable[[CaseState], Any] | None = None,
    postprocess: Callable[[Any], Any] | None = None,
    heartbeat_sec: float = 5.0,
) -> AsyncIterator[dict]:
    """Shared stale → SSE → LLM → files → meta loop for Word/Excel artifacts.

    Domain code stays in the flow: `compose` talks to the model, `write`
    builds the document. Timeout, force, and meta persistence live here so
    a new artifact cannot drift from the others.

    `compose` may be an async generator: yield `ComposeNotice` for extra
    status lines, then yield the model result as the last non-notice value.
    """
    timer = ElapsedTimer()
    elapsed = timer.ms

    yield sse_status(elapsed(), start_message)
    state = load_state(case_id)
    if inspect is not None:
        inspect(state)

    cached = reuse_artifact_events(
        case_id,
        spec,
        force=force,
        stale=is_stale(state),
        already_message=already_message,
        elapsed_ms=elapsed(),
    )
    if cached:
        for event in cached:
            yield event
        return

    ctx: Any = None
    if prepare is not None:
        if prepare_message:
            yield sse_status(elapsed(), prepare_message)
        ctx = prepare(state)

    message = compose_message(state, ctx) if callable(compose_message) else compose_message
    yield sse_status(elapsed(), message)
    try:
        result = None
        if isasyncgenfunction(compose):
            async for item in compose(state, ctx):
                if isinstance(item, ComposeNotice):
                    yield sse_status(elapsed(), item.message)
                else:
                    result = item
        else:
            async for event in _await_compose_with_heartbeat(
                compose(state, ctx),
                elapsed=elapsed,
                message=message,
                heartbeat_sec=heartbeat_sec,
            ):
                if event.get("type") == "status":
                    yield event
                else:
                    result = event.get("result")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"{compose_fail}: {exc}") from exc
    if empty_error is not None and isinstance(result, str) and not result.strip():
        raise ValueError(empty_error)
    if postprocess is not None:
        result = postprocess(result)

    paths = artifact_paths(case_id, state.inspection_name, spec)
    yield sse_status(elapsed(), writing_message)
    outcome = write(state, paths, result, ctx)
    sources_payload = outcome.sources if outcome.sources_file is None else outcome.sources_file
    write_sources_json(paths.sources, sources_payload)
    extra = dict(outcome.extra or {})
    extra["built_elapsed_ms"] = elapsed()
    meta = save_artifact_meta(
        state,
        spec,
        docx=paths.primary,
        md=paths.md,
        sources=outcome.sources,
        body=outcome.body,
        extra=extra,
    )
    yield sse_result(elapsed(), meta, digest=outcome.digest)


async def _await_compose_with_heartbeat(
    coro: Awaitable[Any],
    *,
    elapsed: Callable[[], int],
    message: str,
    heartbeat_sec: float,
) -> AsyncIterator[dict[str, Any]]:
    async def _run() -> Any:
        return await coro

    task: asyncio.Task[Any] = asyncio.create_task(_run())
    try:
        while not task.done():
            timeout = heartbeat_sec if heartbeat_sec > 0 else None
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except asyncio.TimeoutError:
                yield sse_status(elapsed(), message)
        yield {"type": "compose", "result": task.result()}
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
