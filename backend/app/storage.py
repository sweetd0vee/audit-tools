from __future__ import annotations

import json
import re
import zipfile
from datetime import datetime
from pathlib import Path

from app.config import settings
from app.models import CaseState, CaseStatus, new_id


class CaseStore:
    """Filesystem store for audit cases (no DB required for step 1)."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or settings.data_root
        self.root.mkdir(parents=True, exist_ok=True)

    def _case_dir(self, case_id: str) -> Path:
        return self.root / case_id

    def _state_path(self, case_id: str) -> Path:
        return self._case_dir(case_id) / "case.json"

    def library_dir(self, case_id: str) -> Path:
        path = self._case_dir(case_id) / "knowledge_raw"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def archive_path(self, case_id: str) -> Path:
        return self._case_dir(case_id) / "library.zip"

    @staticmethod
    def archive_filename(inspection_name: str, case_id: str) -> str:
        base = re.sub(r"[^\w\u0400-\u04FF\-]+", "_", inspection_name or "", flags=re.UNICODE)
        base = base.strip("_")[:60] or "library"
        return f"{base}_{case_id}.zip"

    def write_library_archive(self, case_id: str) -> Path | None:
        """Pack downloaded files (+ manifest) into library.zip. Returns path or None."""
        lib = self.library_dir(case_id)
        files = [p for p in sorted(lib.iterdir()) if p.is_file()]
        if not files:
            return None

        dest = self.archive_path(case_id)
        if dest.exists():
            dest.unlink()

        with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in files:
                zf.write(path, arcname=path.name)
            manifest = self._case_dir(case_id) / "manifest.json"
            if manifest.exists():
                zf.write(manifest, arcname="manifest.json")
        return dest

    def create(
        self,
        inspection_name: str,
        keywords: list[str],
        period: str | None = None,
        notes: str | None = None,
    ) -> CaseState:
        case_id = new_id()
        case_dir = self._case_dir(case_id)
        case_dir.mkdir(parents=True, exist_ok=True)
        (case_dir / "knowledge_raw").mkdir(exist_ok=True)

        state = CaseState(
            case_id=case_id,
            status=CaseStatus.created,
            inspection_name=inspection_name.strip(),
            keywords=[k.strip() for k in keywords if k.strip()],
            period=period,
            notes=notes,
        )
        self.save(state)
        return state

    def save(self, state: CaseState) -> None:
        state.updated_at = datetime.utcnow()
        path = self._state_path(state.case_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(state.model_dump_json(indent=2), encoding="utf-8")

    def get(self, case_id: str) -> CaseState:
        path = self._state_path(case_id)
        if not path.exists():
            raise FileNotFoundError(f"Case not found: {case_id}")
        return CaseState.model_validate_json(path.read_text(encoding="utf-8"))

    def list_cases(self) -> list[CaseState]:
        cases: list[CaseState] = []
        for child in sorted(self.root.iterdir(), reverse=True):
            state_path = child / "case.json"
            if state_path.exists():
                cases.append(CaseState.model_validate_json(state_path.read_text(encoding="utf-8")))
        return cases

    def write_manifest(self, case_id: str, payload: dict) -> Path:
        path = self._case_dir(case_id) / "manifest.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path


store = CaseStore()
