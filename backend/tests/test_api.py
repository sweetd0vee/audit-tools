from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import __version__
from app.main import app
from app.storage import store


class TestHealthAndCases(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._prev = store.root
        store.root = Path(self.tmp.name)
        store.root.mkdir(parents=True, exist_ok=True)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        store.root = self._prev
        self.tmp.cleanup()

    def test_health_ok(self):
        for path in ("/health", "/api/v1/health"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            body = response.json()
            self.assertEqual(body["status"], "ok")
            self.assertEqual(body["version"], __version__)

    def test_create_and_get_case(self):
        created = self.client.post(
            "/api/v1/cases",
            json={"inspection_name": "Проверка аренды", "keywords": ["аренда"]},
        )
        self.assertEqual(created.status_code, 200)
        case_id = created.json()["case_id"]
        self.assertRegex(case_id, r"^[a-f0-9]{12}$")
        got = self.client.get(f"/api/v1/cases/{case_id}")
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.json()["inspection_name"], "Проверка аренды")

    def test_invalid_case_id_is_400(self):
        response = self.client.get("/api/v1/cases/has.dot")
        self.assertEqual(response.status_code, 400)

    def test_missing_case_is_404(self):
        response = self.client.get("/api/v1/cases/aaaaaaaaaaaa")
        self.assertEqual(response.status_code, 404)

    def test_get_knowledge_does_not_ingest(self):
        created = self.client.post(
            "/api/v1/cases",
            json={"inspection_name": "Проверка аренды", "keywords": ["аренда"]},
        )
        case_id = created.json()["case_id"]
        with patch("app.routers.knowledge.ingest_library") as ingest:
            response = self.client.get(f"/api/v1/cases/{case_id}/knowledge")
        self.assertEqual(response.status_code, 200)
        ingest.assert_not_called()
        self.assertEqual(response.json()["items"], [])

    def test_upload_rejects_bin(self):
        created = self.client.post(
            "/api/v1/cases",
            json={"inspection_name": "Проверка аренды", "keywords": ["аренда"]},
        )
        case_id = created.json()["case_id"]
        response = self.client.post(
            f"/api/v1/cases/{case_id}/knowledge/upload",
            files={"files": ("secret.bin", b"xxxx", "application/octet-stream")},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["added"], [])
        self.assertTrue(body["errors"])
        self.assertIn("bin", body["errors"][0]["error"].lower())


if __name__ == "__main__":
    unittest.main()
