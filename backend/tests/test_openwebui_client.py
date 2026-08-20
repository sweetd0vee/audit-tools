import unittest

from app.services.openwebui_client import _as_items, _id_of, _status_of


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


if __name__ == "__main__":
    unittest.main()
