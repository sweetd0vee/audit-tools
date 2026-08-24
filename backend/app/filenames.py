from __future__ import annotations

import re
from pathlib import Path

_SLUG_RE = re.compile(r"[^\w\u0400-\u04FF\-]+", flags=re.UNICODE)


def slugify(text: str, *, limit: int = 80, fallback: str = "document") -> str:
    base = _SLUG_RE.sub("_", text or "").strip("_")
    return base[:limit] or fallback


def safe_stem(name: str, *, limit: int = 80, fallback: str = "document") -> str:
    return slugify(Path(name).stem, limit=limit, fallback=fallback)
