from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.services.knowledge_ingest import inbox_dir, ingest_inbox
from app.storage import store


class TestKnowledgeUpload(unittest.TestCase):
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

    def _case(self) -> str:
        created = self.client.post(
            "/api/v1/cases",
            json={"inspection_name": "Проверка аренды", "keywords": ["аренда"]},
        )
        self.assertEqual(created.status_code, 200)
        return created.json()["case_id"]

    def test_create_makes_inbox_readme(self):
        case_id = self._case()
        readme = store.inbox_dir(case_id) / "README.txt"
        self.assertTrue(readme.exists())
        self.assertIn("загрузи", readme.read_text(encoding="utf-8"))

    def test_upload_txt_indexes_chunks(self):
        case_id = self._case()
        text = "Статья 1. Внутренняя политика банка по аренде.\n" * 8
        response = self.client.post(
            f"/api/v1/cases/{case_id}/knowledge/upload",
            files={"files": ("policy.txt", text.encode("utf-8"), "text/plain")},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["added"]), 1)
        self.assertEqual(body["added"][0]["source"], "uploaded")
        self.assertEqual(body["added"][0]["extract_status"], "ok")
        self.assertGreater(body["chunks"], 0)
        self.assertTrue(any(item["source"] == "uploaded" for item in body["items"]))
        index = json.loads(
            (store.case_dir(case_id) / "knowledge_index.json").read_text(encoding="utf-8")
        )
        self.assertTrue(index.get("chunks"))

    def test_inbox_ingest_moves_file_and_indexes(self):
        case_id = self._case()
        inbox = inbox_dir(case_id)
        (inbox / "local_act.txt").write_text(
            "Пункт 1. Локальный акт банка, которого нет на pravo.by.",
            encoding="utf-8",
        )
        added = ingest_inbox(case_id)
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0].source, "uploaded")
        self.assertFalse((inbox / "local_act.txt").exists())
        self.assertTrue((inbox / "imported" / "local_act.txt").exists())

        indexed = self.client.post(f"/api/v1/cases/{case_id}/knowledge/index")
        self.assertEqual(indexed.status_code, 200)
        body = indexed.json()
        self.assertTrue(any(item["source"] == "uploaded" for item in body["items"]))
        self.assertGreaterEqual(body["chunks"], 1)

    def test_inbox_skips_readme_and_xlsx(self):
        case_id = self._case()
        inbox = inbox_dir(case_id)
        (inbox / "notes.xlsx").write_bytes(b"PK\x03\x04not-an-npa")
        added = ingest_inbox(case_id)
        self.assertEqual(added, [])
        self.assertTrue((inbox / "README.txt").exists())
        self.assertTrue((inbox / "notes.xlsx").exists())

    def test_library_lists_uploaded_and_inbox_dir(self):
        case_id = self._case()
        self.client.post(
            f"/api/v1/cases/{case_id}/knowledge/upload",
            files={"files": ("inner.md", b"# Policy\ntext", "text/markdown")},
        )
        lib = self.client.get(f"/api/v1/cases/{case_id}/library")
        self.assertEqual(lib.status_code, 200)
        body = lib.json()
        self.assertTrue(body["inbox_dir"])
        self.assertEqual(len(body["uploaded"]), 1)
        self.assertIn("inner", body["uploaded"][0]["filename"])


if __name__ == "__main__":
    unittest.main()
