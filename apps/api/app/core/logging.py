"""Structured JSON logs with redaction (F-11.R3, FR-030, research R3).

One JSON line per record on stdout (Vercel captures it) for the `ledgerline` and `app` loggers;
the request middleware writes exactly one line per HTTP request with `at, run_id, endpoint,
method, status, duration_ms, error_code`. Third-party WARNING+ records reach a redacted JSON
handler on stderr through the root logger.
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
"""Parent of the Ledgerline API loggers (INFO, not propagating: one stdout line per record)."""
PARENTS = (LOGGER, "app")
"""Loggers that get the JSON stdout handler; `app` keeps propagating (pytest caplog) and its
level unset (WARNING inherited from root), so scripts' stdout stays clean."""
REQUEST_LOGGER = "ledgerline.request"
FILTERED_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx")
"""Loggers with their own handlers that also get the filter at logger level. A logger-level
filter only sees records logged on that exact logger, not on its children (`uvicorn.asgi`
is not covered by `uvicorn`); the handler-level filter on our stdout and stderr handlers
covers whatever reaches them."""
UNMATCHED = "<unmatched>"
"""`endpoint` when no route matched: the raw path is never logged."""

_BEARER = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_RUN_PARAM = re.compile(r"/runs/\{(\w+)\}")
_MAX_ERROR_BODY = 64 * 1024


class RedactionFilter(logging.Filter):
    """Rewrites the record in place (message, args, exception and stack text); never drops it."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self.set_secrets(secrets)

    def set_secrets(self, secrets: Iterable[str]) -> None:
        """Replace (not extend) the secret values to redact."""
        values = sorted({s for s in secrets if s}, key=len, reverse=True)
        self._secrets = re.compile("|".join(map(re.escape, values))) if values else None

    def redact(self, text: str) -> str:
        if self._secrets is not None:
            text = self._secrets.sub(REDACTED, text)
        return _BEARER.sub(REDACTED, text)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # bad format args must never raise into the caller
            message = f"{record.msg!s} {record.args!r}"
        record.msg, record.args = self.redact(message), None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self.redact(record.stack_info)
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per record; request records carry exactly the R3 fields. `redact`, when
    given, is applied to every string value before encoding, so the line stays valid JSON."""

    def __init__(self, redact: Callable[[str], str] | None = None) -> None:
        super().__init__()
        self._redact = redact

    def format(self, record: logging.LogRecord) -> str:
        payload = self._payload(record)
        if self._redact is not None:
            redact = self._redact
            payload = {k: redact(v) if isinstance(v, str) else v for k, v in payload.items()}
        return json.dumps(payload, default=str)

    def _payload(self, record: logging.LogRecord) -> dict[str, Any]:
        request = getattr(record, "request", None)
        if isinstance(request, dict):
            return dict(request)
        payload: dict[str, Any] = {
            # Scenario clock, not `record.created`: every timestamp is on the scenario day
            # (F-11.R1, AC1), and a frozen clock keeps log lines replayable.
            "at": _now_iso(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            payload["exc"] = record.exc_text
        return payload


class _StdHandler(logging.StreamHandler[TextIO]):
    """JSON lines to the current `sys.stdout` or `sys.stderr` (a swapped stream, e.g. in
    tests, is followed), redacted by the shared filter."""

    def __init__(self, target: str, level: int = logging.NOTSET) -> None:
        self._target = target
        super().__init__()
        self.setLevel(level)
        self.setFormatter(JsonFormatter(_filter.redact))
        self.addFilter(_filter)

    @property
    def stream(self) -> TextIO:
        stream: TextIO = getattr(sys, self._target)
        return stream

    @stream.setter
    def stream(self, value: TextIO) -> None:
        pass


_filter = RedactionFilter()
_stdout = _StdHandler("stdout")
_stderr = _StdHandler("stderr", logging.WARNING)


def _not_on_stdout(record: logging.LogRecord) -> bool:
    """`app.*` records propagate to root after `_stdout` wrote them: one line, not two."""
    return not any(record.name == p or record.name.startswith(p + ".") for p in PARENTS)


_stderr.addFilter(_not_on_stdout)


def configure_logging(settings: Settings) -> None:
    """Install the JSON stdout handler on `PARENTS`, the WARNING stderr handler on the root
    logger and the logger-level filters; safe to call repeatedly (each call only refreshes
    the secret values)."""
    _filter.set_secrets(settings.secret_values())
    ledgerline = logging.getLogger(LOGGER)
    ledgerline.setLevel(logging.INFO)
    ledgerline.propagate = False  # exactly one line per record on stdout
    for name in PARENTS:
        logger = logging.getLogger(name)
        if _stdout not in logger.handlers:
            logger.addHandler(_stdout)
    root = logging.getLogger()
    if _stderr not in root.handlers:
        root.addHandler(_stderr)
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
    route = getattr(scope.get("route"), "path", None)
    run_id: str | None = None
    if route is not None:  # the run id is a path parameter of a matched `/runs/{...}` route
        param = _RUN_PARAM.search(route)
        value = scope.get("path_params", {}).get(param.group(1)) if param else None
        run_id = str(value) if value is not None else None
    logging.getLogger(REQUEST_LOGGER).info(
        "request",
        extra={
            "request": {
                "at": _now_iso(),
                "run_id": run_id,
                "endpoint": route or UNMATCHED,
                "method": scope.get("method"),
                "status": status,
                "duration_ms": round((time.monotonic() - start) * 1000),
                "error_code": error_code,
            }
        },
    )
