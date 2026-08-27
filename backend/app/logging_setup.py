from __future__ import annotations

import logging
import sys
import time
import uuid
from contextvars import ContextVar

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import settings

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] [%(request_id)s] %(message)s"
_QUIET_LOGGERS = ("httpx", "httpcore", "httpcore.http11", "httpcore.connection")
_NOISY_PATHS = {"/", "/health", "/api/v1/health", "/docs", "/openapi.json", "/redoc"}


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get("-")
        return True


def configure_logging() -> None:
    """Idempotent: stdout, request id on every record, httpx quiet."""
    level_name = (settings.log_level or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    formatter = logging.Formatter(LOG_FORMAT)
    filt = RequestIdFilter()

    root = logging.getLogger()
    root.setLevel(level)
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(formatter)
        handler.addFilter(filt)
        root.addHandler(handler)
    else:
        for existing in root.handlers:
            existing.addFilter(filt)
            if existing.formatter is None:
                existing.setFormatter(formatter)

    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


class RequestLogMiddleware:
    """Pure ASGI so SSE streams are not buffered (unlike BaseHTTPMiddleware)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._log = logging.getLogger("app.http")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path") or "")
        method = str(scope.get("method") or "")
        header_map = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in (scope.get("headers") or [])
        }
        incoming = (header_map.get("x-request-id") or "").strip()
        rid = incoming[:64] if incoming else uuid.uuid4().hex[:12]
        token = request_id_var.set(rid)
        started = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message.get("status") or 500)
                headers = list(message.get("headers") or [])
                headers.append((b"x-request-id", rid.encode("ascii")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            self._log.log(
                logging.DEBUG if path in _NOISY_PATHS else logging.INFO,
                "%s %s %s %sms",
                method,
                path,
                status_code,
                elapsed_ms,
            )
        except Exception:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            self._log.exception("%s %s failed after %sms", method, path, elapsed_ms)
            raise
        finally:
            request_id_var.reset(token)
