from __future__ import annotations

from urllib.parse import urlparse

from app.config import settings


def host_allowed(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in settings.domain_allowlist)
