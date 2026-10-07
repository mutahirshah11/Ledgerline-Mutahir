"""Telegram sender (contracts/alerts.md; F-10.R5, F-11.R4, C21, research R3/R7/R8).

The bot token is part of the request URL, so no URL and no httpx error text ever leaves this
module: errors carry only a cause word, and httpx's own request log line is redacted here.
"""

import logging
import mimetypes
import re
import time
from collections.abc import Callable
from typing import Any, Protocol, Self

import httpx

from app.core.config import Settings, get_settings
from app.core.errors import UpstreamFailed
from app.domain.enums import Fault
from app.services.runlog import OnRetry, retrying

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
CAPTION_MAX = 1024
"""Bot API `sendDocument` caption limit (characters)."""
_MIME = mimetypes.MimeTypes()
"""Python's built-in table only (the global `mimetypes` also reads the Windows registry)."""


class Notifier(Protocol):
    """Sends a plain-text message or a document; returns Telegram's `message_id`. Raises
    UpstreamFailed("telegram", detail) after its own retries; the detail never contains the
    bot token."""

    def send_message(self, chat_id: str, text: str) -> int: ...

    def send_document(
        self, chat_id: str, filename: str, content: bytes, caption: str | None = None
    ) -> int: ...


class _RedactBotPath(logging.Filter):
    """Replaces `/bot<token>` in httpx log lines (F-11.AC4); the record is kept, not dropped."""

    _BOT_PATH = re.compile(r"/bot[^/\s\"']+")

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "/bot" in message:
            record.msg, record.args = self._BOT_PATH.sub("/bot[REDACTED]", message), None
        return True


def _install_log_filter() -> None:
    """Add the redaction filter to the `httpx` logger once (it logs the request URL)."""
    httpx_log = logging.getLogger("httpx")
    if not any(isinstance(f, _RedactBotPath) for f in httpx_log.filters):
        httpx_log.addFilter(_RedactBotPath())


class _Retryable(Exception):
    """A transient failure (R8: timeouts, connection errors, 429, 5xx); `cause` is one word."""

    def __init__(self, cause: str) -> None:
        super().__init__(cause)
        self.cause = cause


def _cause_of(error: BaseException) -> str | None:
    """Only `_Retryable` retries; `_post` already turned every httpx error into a cause word."""
    return error.cause if isinstance(error, _Retryable) else None


class TelegramNotifier:
    """`sendMessage` as plain text (no `parse_mode`) and `sendDocument` (multipart), each with
    `TELEGRAM_ATTEMPTS` attempts, the `TELEGRAM_BACKOFF_S` waits and `TELEGRAM_TIMEOUT_S` per
    call. 4xx answers are not retried.
    `fault=TELEGRAM_DOWN` fails every attempt without a network call (Section 25)."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        fault: Fault | None = None,
        on_retry: OnRetry | None = None,
    ) -> None:
        _install_log_filter()
        self._settings = settings or get_settings()
        self._client = client or httpx.Client()
        self._sleep = sleep
        self._fault = fault
        self._on_retry = on_retry

    def close(self) -> None:
        """Close the HTTP client (also an injected one)."""
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def send_message(self, chat_id: str, text: str) -> int:
        """Plain text, link previews off (research R11: the `Report:` link stays a line)."""
        body = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        return self._send("sendMessage", {"json": body})

    def send_document(
        self, chat_id: str, filename: str, content: bytes, caption: str | None = None
    ) -> int:
        """`sendDocument` as multipart (005 research R11; F-10 sends `report.html`), content
        type from the filename. A caption over `CAPTION_MAX` is a caller bug: ValueError before
        any call, never a retry."""
        if caption is not None and len(caption) > CAPTION_MAX:
            raise ValueError(f"caption is {len(caption)} characters; Telegram allows {CAPTION_MAX}")
        data = {"chat_id": chat_id} | ({} if caption is None else {"caption": caption})
        mime = _MIME.guess_type(filename)[0] or "application/octet-stream"
        files = {"document": (filename, content, mime)}
        return self._send("sendDocument", {"data": data, "files": files})

    def _send(self, method: str, body: dict[str, Any]) -> int:
        """One Bot API call with the retry policy; `body` is httpx's `post` keyword args."""
        token = self._settings.TELEGRAM_BOT_TOKEN
        if token is None or not token.get_secret_value():
            raise UpstreamFailed("telegram", "bot token not set")
        url = f"{TELEGRAM_API}/bot{token.get_secret_value()}/{method}"
        policy = retrying(
            "telegram",
            max(1, self._settings.TELEGRAM_ATTEMPTS),
            self._settings.TELEGRAM_BACKOFF_S,
            self._retry_heard,
            sleep=self._sleep,
            chain=False,
            cause_of=_cause_of,
        )
        return policy(lambda: self._post(url, body))

    def _retry_heard(self, detail: dict[str, object]) -> None:
        log.warning("telegram send failed; retrying", extra=detail)
        if self._on_retry is not None:
            self._on_retry(detail)

    def _post(self, url: str, body: dict[str, Any]) -> int:
        """One call: the message id, `_Retryable`, or UpstreamFailed for a final rejection.
        Raised errors never chain the httpx exception (its text can hold the URL)."""
        if self._fault is Fault.TELEGRAM_DOWN:
            raise _Retryable("down")
        try:
            response = self._client.post(url, timeout=self._settings.TELEGRAM_TIMEOUT_S, **body)
        except httpx.TimeoutException:
            raise _Retryable("timeout") from None
        except httpx.TransportError:
            raise _Retryable("connection error") from None
        status = response.status_code
        if status == 429:
            raise _Retryable("rate limited")
        if status >= 500:
            raise _Retryable("server error")
        if status >= 400:
            raise UpstreamFailed("telegram", f"rejected: HTTP {status}")
        try:
            answer = response.json()
            message_id = answer["result"]["message_id"]
        except (ValueError, KeyError, TypeError):
            raise UpstreamFailed("telegram", "rejected: unexpected answer") from None
        if answer.get("ok") is not True or not isinstance(message_id, int):
            raise UpstreamFailed("telegram", "rejected: unexpected answer")
        return message_id
