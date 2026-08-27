from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app.config import settings
from app.services.allowlist_http import DisallowedHost, allowlisted_get
from app.services.downloader import download_url
from app.services.npa_search import _search_engines


def _npa_html() -> bytes:
    return ("<html><body><p>" + ("Статья 1 текст нормы. " * 80) + "</p></body></html>").encode()


class TestAllowlistedGet(unittest.IsolatedAsyncioTestCase):
    async def test_redirect_off_allowlist_never_fetched(self):
        fetched: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            fetched.append(str(request.url))
            if request.url.host == "pravo.by":
                return httpx.Response(
                    302,
                    headers={"Location": "https://evil.example/steal.pdf"},
                )
            return httpx.Response(200, content=b"stolen")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
            with self.assertRaises(DisallowedHost) as ctx:
                await allowlisted_get(client, "https://pravo.by/document/?guid=3871&p0=hk9800218")
        self.assertIn("evil.example", str(ctx.exception))
        self.assertEqual(fetched, ["https://pravo.by/document/?guid=3871&p0=hk9800218"])

    async def test_redirect_on_allowlist_follows(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/go":
                return httpx.Response(
                    302,
                    headers={"Location": "https://pravo.by/document/?guid=3871&p0=hk9800218"},
                )
            return httpx.Response(200, content=b"ok-body", headers={"content-type": "text/html"})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
            resp = await allowlisted_get(client, "https://pravo.by/go")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"ok-body")
        self.assertEqual(str(resp.url), "https://pravo.by/document/?guid=3871&p0=hk9800218")

    async def test_relative_redirect_stays_on_host(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/go":
                return httpx.Response(302, headers={"Location": "/document/?guid=3871&p0=x"})
            return httpx.Response(200, content=b"ok")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
            resp = await allowlisted_get(client, "https://pravo.by/go")
        self.assertEqual(str(resp.url), "https://pravo.by/document/?guid=3871&p0=x")

    async def test_protocol_relative_redirect_off_allowlist(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"Location": "//evil.example/x"})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
            with self.assertRaises(DisallowedHost):
                await allowlisted_get(client, "https://pravo.by/go")


class TestDownloadRedirect(unittest.IsolatedAsyncioTestCase):
    async def test_download_aborts_when_redirect_leaves_allowlist(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "pravo.by":
                return httpx.Response(
                    302,
                    headers={"Location": "https://evil.example/doc.pdf"},
                )
            return httpx.Response(200, content=_npa_html(), headers={"content-type": "text/html"})

        transport = httpx.MockTransport(handler)

        def client_factory(*args, **kwargs):
            kwargs.pop("follow_redirects", None)
            return httpx.AsyncClient(transport=transport, follow_redirects=False, **kwargs)

        with patch("app.services.downloader.httpx.AsyncClient", side_effect=client_factory):
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(DisallowedHost):
                    await download_url(
                        "https://pravo.by/document/?guid=3871&p0=hk9800218",
                        Path(tmp),
                        "ГК",
                        1,
                    )
                self.assertFalse(list(Path(tmp).iterdir()))

    async def test_download_follows_allowlisted_redirect(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/go":
                return httpx.Response(
                    302,
                    headers={"Location": "https://pravo.by/document/?guid=3871&p0=hk9800218"},
                )
            return httpx.Response(
                200,
                content=_npa_html(),
                headers={"content-type": "text/html"},
            )

        transport = httpx.MockTransport(handler)

        def client_factory(*args, **kwargs):
            kwargs.pop("follow_redirects", None)
            return httpx.AsyncClient(transport=transport, follow_redirects=False, **kwargs)

        with patch("app.services.downloader.httpx.AsyncClient", side_effect=client_factory):
            with tempfile.TemporaryDirectory() as tmp:
                result = await download_url(
                    "https://pravo.by/go?x=1",
                    Path(tmp),
                    "ГК",
                    1,
                )
        self.assertGreater(result["bytes"], 800)
        self.assertIn("pravo.by", result["url"])


class TestWebFallback(unittest.IsolatedAsyncioTestCase):
    async def test_off_does_not_call_ddg_or_bing(self):
        searx = AsyncMock(return_value=[])
        html = AsyncMock(return_value=[])
        with (
            patch.object(settings, "npa_web_fallback", False),
            patch("app.services.npa_search._from_searxng", searx),
            patch("app.services.npa_search._from_html_search", html),
        ):
            await _search_engines("site:pravo.by гражданский кодекс")
        searx.assert_awaited()
        html.assert_not_called()

    async def test_on_calls_ddg_and_bing(self):
        searx = AsyncMock(return_value=[])
        html = AsyncMock(return_value=[])
        with (
            patch.object(settings, "npa_web_fallback", True),
            patch("app.services.npa_search._from_searxng", searx),
            patch("app.services.npa_search._from_html_search", html),
            patch("app.services.npa_search._fallback_warned", True),
        ):
            await _search_engines("site:pravo.by гражданский кодекс")
        searx.assert_awaited()
        self.assertEqual(html.await_count, 2)
        sources = {call.args[0] for call in html.await_args_list}
        self.assertEqual(sources, {"duckduckgo", "bing"})


class TestCors(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        from app.main import app

        self.client = TestClient(app)

    def test_open_webui_origin_allowed(self):
        resp = self.client.options(
            "/",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
        self.assertEqual(resp.headers.get("access-control-allow-origin"), "http://localhost:3000")

    def test_foreign_origin_not_star(self):
        resp = self.client.options(
            "/",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        allow = resp.headers.get("access-control-allow-origin")
        self.assertNotEqual(allow, "*")
        self.assertNotEqual(allow, "https://evil.example")


if __name__ == "__main__":
    unittest.main()
