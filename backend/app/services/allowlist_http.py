"""HTTP GET that will not follow a redirect off the NPA domain allowlist."""

from __future__ import annotations

from urllib.parse import urljoin

import httpx

from app.domains import host_allowed

MAX_REDIRECTS = 20


class DisallowedHost(ValueError):
    """Request target is outside the NPA domain allowlist."""


def require_allowed_url(url: str) -> None:
    if not host_allowed(url):
        raise DisallowedHost(f"Domain not allowed: {url}")


async def allowlisted_get(
    client: httpx.AsyncClient,
    url: str,
    **kwargs,
) -> httpx.Response:
    """GET ``url``, following redirects only while every hop stays on the allowlist.

    Extra kwargs (params, headers, …) apply to the first request only, so a
    redirect target is not called with the original query string appended.
    """
    kwargs.pop("follow_redirects", None)
    current = url
    extra = kwargs
    for _ in range(MAX_REDIRECTS):
        require_allowed_url(current)
        response = await client.get(current, follow_redirects=False, **extra)
        extra = {}
        if not response.is_redirect:
            return response
        location = response.headers.get("location")
        await response.aclose()
        if not location:
            raise DisallowedHost(f"Redirect without Location from {current}")
        current = urljoin(str(response.url), location)
    raise DisallowedHost(f"Too many redirects for {url}")
