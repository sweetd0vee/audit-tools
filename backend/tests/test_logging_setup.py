import logging
import unittest

from app.logging_setup import RequestIdFilter, configure_logging, request_id_var


class TestLoggingSetup(unittest.TestCase):
    def test_request_id_on_records(self):
        configure_logging()
        token = request_id_var.set("abc123")
        try:
            record = logging.LogRecord(
                name="app.test",
                level=logging.INFO,
                pathname=__file__,
                lineno=1,
                msg="hello",
                args=(),
                exc_info=None,
            )
            self.assertTrue(RequestIdFilter().filter(record))
            self.assertEqual(record.request_id, "abc123")
        finally:
            request_id_var.reset(token)

    def test_configure_is_idempotent(self):
        configure_logging()
        before = len(logging.getLogger().handlers)
        configure_logging()
        self.assertEqual(len(logging.getLogger().handlers), before)
