from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from app.storage import CaseStore, InvalidCaseId, atomic_write_text, validate_case_id


class TestCaseId(unittest.TestCase):
    def test_accepts_demo_and_hex(self):
        self.assertEqual(validate_case_id("c1"), "c1")
        self.assertEqual(validate_case_id("3a23fb6db4a9"), "3a23fb6db4a9")

    def test_rejects_path_traversal(self):
        for bad in ("../etc", "..\\windows", "a/b", "has.dot", "", "x" * 40):
            with self.assertRaises(InvalidCaseId):
                validate_case_id(bad)


class TestAtomicSave(unittest.TestCase):
    def test_get_rejects_traversal_before_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CaseStore(root=Path(tmp))
            with self.assertRaises(InvalidCaseId):
                store.get("../etc")

    def test_save_roundtrip_and_no_tmp_left(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CaseStore(root=Path(tmp))
            state = store.create("Проверка аренды", ["аренда"])
            state.notes = "черновик"
            store.save(state)
            loaded = store.get(state.case_id)
            self.assertEqual(loaded.notes, "черновик")
            leftovers = list(store.case_dir(state.case_id).glob("*.tmp"))
            self.assertEqual(leftovers, [])

    def test_concurrent_saves_leave_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CaseStore(root=Path(tmp))
            state = store.create("Проверка аренды", ["аренда"])
            errors: list[BaseException] = []

            def writer(n: int) -> None:
                try:
                    current = store.get(state.case_id)
                    current.notes = str(n)
                    store.save(current)
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=writer, args=(i,)) for i in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            path = store.case_dir(state.case_id) / "case.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["case_id"], state.case_id)
            self.assertIn("notes", payload)

    def test_atomic_write_text_replaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.json"
            atomic_write_text(path, '{"a": 1}')
            atomic_write_text(path, '{"a": 2}')
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"a": 2})
            self.assertFalse(path.with_name("x.json.tmp").exists())

    def test_list_skips_unsafe_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = CaseStore(root=root)
            store.create("Проверка аренды", ["аренда"])
            (root / "not a case").mkdir()
            (root / "has.dot").mkdir()
            cases = store.list_cases()
            self.assertEqual(len(cases), 1)


if __name__ == "__main__":
    unittest.main()
