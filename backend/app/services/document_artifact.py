from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

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
        "built_at": datetime.utcnow().isoformat(),
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
