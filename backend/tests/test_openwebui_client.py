from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import httpx

from app.services.knowledge_owui import collection_display_name
from app.services.openwebui_client import _as_items, _id_of, _status_of, ensure_collection

_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _client_with_transport(transport: httpx.MockTransport):
    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    return factory


class TestAsItems(unittest.TestCase):
    def test_paginated_wrapper_is_not_iterated_as_rows(self):
        payload = {
            "items": [{"id": "kb1", "name": "Аудит: аренда"}],
            "total": 1,
        }
        self.assertEqual(_as_items(payload), [{"id": "kb1", "name": "Аудит: аренда"}])

    def test_empty_paginated_wrapper(self):
        self.assertEqual(_as_items({"items": [], "total": 0}), [])

    def test_legacy_bare_list(self):
        rows = [{"id": "1", "name": "x"}]
        self.assertEqual(_as_items(rows), rows)

    def test_string_payload_does_not_crash(self):
        self.assertEqual(_as_items("items"), [])
        self.assertIsNone(_id_of(None))
        self.assertIsNone(_status_of(None))

    def test_id_from_nested_data(self):
        self.assertEqual(_id_of({"data": {"id": "file-9"}}), "file-9")
        self.assertEqual(_id_of(["abc"]), "abc")

    def test_status_from_file_record(self):
        self.assertEqual(_status_of({"status": "completed"}), "completed")
        self.assertEqual(
            _status_of({"id": "f1", "data": {"status": "pending"}}),
            "pending",
        )
        self.assertEqual(
            _status_of({"data": {"content": "статья 1"}}),
            "completed",
        )


class TestCollectionDisplayName(unittest.TestCase):
    def test_same_inspection_different_cases_are_unique(self):
        title = "Проверка аренды коммерческой недвижимости"
        a = collection_display_name(title, "aaa111aaa111")
        b = collection_display_name(title, "bbb222bbb222")
        self.assertNotEqual(a, b)
        self.assertIn("aaa111aaa111", a)
        self.assertIn("bbb222bbb222", b)
        self.assertIn(title, a)
        self.assertIn(title, b)

    def test_truncation_keeps_case_id(self):
        title = "Проверка " + ("оченьдлинноеназвание" * 8)
        name = collection_display_name(title, "c1shortid000")
        self.assertLessEqual(len(name), 80)
        self.assertTrue(name.endswith(" · c1shortid000"))
        other = collection_display_name(title, "c2shortid000")
        self.assertNotEqual(name, other)


class TestEnsureCollection(unittest.IsolatedAsyncioTestCase):
    async def test_does_not_reuse_folder_with_same_inspection_name(self):
        title = "Проверка аренды коммерческой недвижимости"
        existing = {"id": "kb-shared", "name": f"Аудит: {title}"}
        unique = collection_display_name(title, "abc123abc123")
        created = {"id": "kb-new", "name": unique}
        posts: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if request.method == "GET" and path.rstrip("/").endswith("/knowledge"):
                return httpx.Response(
                    200, json={"items": [existing], "total": 1}
                )
            if request.method == "POST" and path.endswith("/knowledge/create"):
                posts.append(json.loads(request.content.decode()))
                return httpx.Response(200, json=created)
            return httpx.Response(404, text="no")

        with patch(
            "app.services.openwebui_client.httpx.AsyncClient",
            _client_with_transport(httpx.MockTransport(handler)),
        ):
            got = await ensure_collection(unique, "НПА кейса abc123abc123", "token")
        self.assertEqual(got["id"], "kb-new")
        self.assertEqual(posts[0]["name"], unique)

    async def test_reuses_folder_that_already_has_this_case_id(self):
        title = "Проверка аренды коммерческой недвижимости"
        unique = collection_display_name(title, "abc123abc123")
        existing = {"id": "kb-mine", "name": unique}
        posts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal posts
            path = request.url.path
            if request.method == "GET" and path.rstrip("/").endswith("/knowledge"):
                return httpx.Response(
                    200, json={"items": [existing], "total": 1}
                )
            if request.method == "POST":
                posts += 1
                return httpx.Response(200, json={"id": "should-not"})
            return httpx.Response(404, text="no")

        with patch(
            "app.services.openwebui_client.httpx.AsyncClient",
            _client_with_transport(httpx.MockTransport(handler)),
        ):
            got = await ensure_collection(unique, "НПА кейса abc123abc123", "token")
        self.assertEqual(got["id"], "kb-mine")
        self.assertEqual(posts, 0)


if __name__ == "__main__":
    unittest.main()
