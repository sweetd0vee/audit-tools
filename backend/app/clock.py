from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> datetime:
    """Naive UTC timestamp. Same ISO shape as the old datetime.utcnow()."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
