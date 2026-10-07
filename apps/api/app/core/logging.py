"""Structured JSON logs with redaction (F-11.R3, FR-030, research R3).

One JSON line per record on stdout (Vercel captures it); the request middleware writes exactly
one line per HTTP request with `at, run_id, endpoint, method, status, duration_ms, error_code`.
Bodies and file bytes are never logged. `RedactionFilter` replaces every configured secret value
and any `Bearer …` string with `[REDACTED]`, in the message and the exception text (F-11.AC4).
"""

import json
import logging
import re
import sys
import time
from collections.abc import Callable, Iterable
from typing import Any, TextIO

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core import clock
from app.core.config import Settings

REDACTED = "[REDACTED]"
LOGGER = "ledgerline"
"""Parent of every Ledgerline logger; its one handler writes JSON lines to stdout."""
REQUEST_LOGGER = "ledgerline.request"
FILTERED_LOGGERS = ("ledgerline.api", "uvicorn", "uvicorn.error", "uvicorn.access", "httpx")
"""Loggers with their own handlers (or other propagation) that also get the filter."""

_BEARER = re.compile(r"Bearer\s+\S+", re.IGNORECASE)
_RUN_ID = re.compile(r"/runs/([^/]+)")
_MAX_ERROR_BODY = 64 * 1024


class RedactionFilter(logging.Filter):
    """Rewrites the record in place (message, args, exception and stack text); never drops it."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self.set_secrets(secrets)

    def set_secrets(self, secrets: Iterable[str]) -> None:
        values = sorted({s for s in secrets if s}, key=len, reverse=True)
        self._secrets = re.compile("|".join(map(re.escape, values))) if values else None

    def redact(self, text: str) -> str:
        if self._secrets is not None:
            text = self._secrets.sub(REDACTED, text)
        return _BEARER.sub(REDACTED, text)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg, record.args = self.redact(record.getMessage()), None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self.redact(record.stack_info)
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per record; request records carry exactly the R3 fields. `redact`, when
    given, is applied to the whole line (a raw path could carry a token)."""

    def __init__(self, redact: Callable[[str], str] | None = None) -> None:
        super().__init__()
        self._redact = redact

    def format(self, record: logging.LogRecord) -> str:
        line = self._format(record)
        return self._redact(line) if self._redact is not None else line

    def _format(self, record: logging.LogRecord) -> str:
        request = getattr(record, "request", None)
        if isinstance(request, dict):
            return json.dumps(request, default=str)
        payload: dict[str, Any] = {
            "at": _now_iso(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            payload["exc"] = record.exc_text
        return json.dumps(payload, default=str)


class _StdoutHandler(logging.StreamHandler[TextIO]):
    """Writes to the current `sys.stdout` (so a swapped stream, e.g. in tests, is followed)."""

    def __init__(self) -> None:
        super().__init__(sys.stdout)

    @property
    def stream(self) -> TextIO:
        return sys.stdout

    @stream.setter
    def stream(self, value: TextIO) -> None:
        pass


_filter = RedactionFilter()


def configure_logging(settings: Settings) -> None:
    """Install the JSON stdout handler and the redaction filter; safe to call repeatedly
    (each call only refreshes the secret values)."""
    _filter.set_secrets(settings.secret_values())
    root = logging.getLogger(LOGGER)
    root.setLevel(logging.INFO)
    root.propagate = False  # exactly one line per record on stdout
    if not any(isinstance(h, _StdoutHandler) for h in root.handlers):
        handler = _StdoutHandler()
        handler.setFormatter(JsonFormatter(_filter.redact))
        handler.addFilter(_filter)
        root.addHandler(handler)
    for name in FILTERED_LOGGERS:
        logger = logging.getLogger(name)
        if _filter not in logger.filters:
            logger.addFilter(_filter)


def _now_iso() -> str | None:
    try:
        return clock.now().isoformat()
    except Exception:  # a log line must never fail on the clock
        return None


def _error_code(body: bytes) -> str | None:
    try:
        code = json.loads(body)["error"]["code"]
    except Exception:
        return None
    return code if isinstance(code, str) else None


class RequestLogMiddleware:
    """ASGI middleware: one JSON line per HTTP request; only an error body is read, for its code."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start = time.monotonic()
        status = 500
        body = bytearray()

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body" and status >= 400:
                if len(body) < _MAX_ERROR_BODY:
                    body.extend(message.get("body", b""))
            await send(message)

        error_code: str | None = None
        try:
            await self.app(scope, receive, send_wrapper)
            if status >= 400:
                error_code = _error_code(bytes(body))
        except Exception:
            status, error_code = 500, "INTERNAL_ERROR"  # the outer handler answers 500
            raise
        finally:
            _log_request(scope, status, error_code, start)


def _log_request(scope: Scope, status: int, error_code: str | None, start: float) -> None:
    path: str = scope.get("path", "")
    route = scope.get("route")
    run_id = _RUN_ID.search(path)
    logging.getLogger(REQUEST_LOGGER).info(
        "request",
        extra={
            "request": {
                "at": _now_iso(),
                "run_id": run_id.group(1) if run_id else None,
                "endpoint": getattr(route, "path", None) or path,
                "method": scope.get("method"),
                "status": status,
                "duration_ms": round((time.monotonic() - start) * 1000),
                "error_code": error_code,
            }
        },
    )
