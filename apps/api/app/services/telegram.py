"""Telegram sender (contracts/alerts.md; F-10.R5, F-11.R4, C21, research R3/R7/R8).

The bot token is part of the request URL, so no URL and no httpx error text ever leaves this
module: errors carry only a cause word, and httpx's own request log line is redacted here.
"""

import logging
import re
import time
from collections.abc import Callable
from typing import Protocol

import httpx

from app.core.config import Settings, get_settings
from app.core.errors import UpstreamFailed
from app.domain.enums import Fault

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


class Notifier(Protocol):
    """Sends a plain-text message; returns Telegram's `message_id`. Raises UpstreamFailed
    ("telegram", detail) after its own retries; the detail never contains the bot token."""

    def send_message(self, chat_id: str, text: str) -> int: ...


class _RedactBotPath(logging.Filter):
    """Replaces `/bot<token>` in httpx log lines (F-11.AC4); the record is kept, not dropped."""

    _BOT_PATH = re.compile(r"/bot[^/\s\"']+")

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "/bot" in message:
            record.msg, record.args = self._BOT_PATH.sub("/bot[REDACTED]", message), None
        return True


_httpx_log = logging.getLogger("httpx")
if not any(isinstance(f, _RedactBotPath) for f in _httpx_log.filters):
    _httpx_log.addFilter(_RedactBotPath())


class _Retryable(Exception):
    """A transient failure (R8: timeouts, connection errors, 429, 5xx); `cause` is one word."""

    def __init__(self, cause: str) -> None:
        super().__init__(cause)
        self.cause = cause


class TelegramNotifier:
    """`sendMessage` as plain text (no `parse_mode`), `TELEGRAM_ATTEMPTS` attempts with the
    `TELEGRAM_BACKOFF_S` waits and `TELEGRAM_TIMEOUT_S` per call. 4xx answers are not retried.
    `fault=TELEGRAM_DOWN` fails every attempt without a network call (Section 25)."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        fault: Fault | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._client = client or httpx.Client()
        self._sleep = sleep
        self._fault = fault

    def send_message(self, chat_id: str, text: str) -> int:
        token = self._settings.TELEGRAM_BOT_TOKEN
        if token is None or not token.get_secret_value():
            raise UpstreamFailed("telegram", "bot token not set")
        url = f"{TELEGRAM_API}/bot{token.get_secret_value()}/sendMessage"
        attempts = max(1, self._settings.TELEGRAM_ATTEMPTS)
        waits = self._settings.TELEGRAM_BACKOFF_S or (0,)
        cause = ""
        for attempt in range(1, attempts + 1):
            try:
                return self._post(url, {"chat_id": chat_id, "text": text})
            except _Retryable as exc:
                cause = exc.cause
            if attempt < attempts:
                log.warning(
                    "telegram send failed; retrying", extra={"cause": cause, "attempt": attempt}
                )
                self._sleep(waits[min(attempt - 1, len(waits) - 1)])
        raise UpstreamFailed("telegram", f"{cause} after {attempts} attempts")

    def _post(self, url: str, payload: dict[str, str]) -> int:
        """One call: the message id, `_Retryable`, or UpstreamFailed for a final rejection.
        Raised errors never chain the httpx exception (its text can hold the URL)."""
        if self._fault is Fault.TELEGRAM_DOWN:
            raise _Retryable("down")
        try:
            response = self._client.post(
                url, json=payload, timeout=self._settings.TELEGRAM_TIMEOUT_S
            )
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
            body = response.json()
            message_id = body["result"]["message_id"]
        except (ValueError, KeyError, TypeError):
            raise UpstreamFailed("telegram", "rejected: unexpected answer") from None
        if body.get("ok") is not True or not isinstance(message_id, int):
            raise UpstreamFailed("telegram", "rejected: unexpected answer")
        return message_id
