"""Telegram sender (contracts/alerts.md; F-10.R5, F-11.R4, C21, research R3/R7/R8) and the
controller summary (F-10.R4 to R8; 005 contracts/telegram-message.md, data-model.md §4, §6).

The bot token is part of the request URL, so no URL and no httpx error text ever leaves this
module: errors carry only a cause word, and httpx's own request log line is redacted here.
"""

import logging
import mimetypes
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol, Self
from uuid import UUID

import httpx

from app.core import clock
from app.core.config import Settings, get_settings
from app.core.db import Repo
from app.core.errors import ApiError, LedgerlineError, UpstreamFailed
from app.domain.enums import (
    AuditAction,
    AuditChannel,
    ErrorCode,
    Fault,
    NotificationStatus,
    Outcome,
    Stage,
)
from app.domain.ids import summary_key
from app.domain.models import Notification, Proof, Run, RunView
from app.pipeline import report
from app.pipeline.engine import ROW_ONLY_OUTCOMES
from app.services.audit import audit
from app.services.runlog import OnRetry, StepLog, retrying, stage_step

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
CAPTION_MAX = 1024
"""Bot API `sendDocument` caption limit (characters)."""
_MIME = mimetypes.MimeTypes()
"""Python's built-in table only (the global `mimetypes` also reads the Windows registry)."""


class Notifier(Protocol):
    """Sends a plain-text message or a document; returns Telegram's `message_id`. Raises
    UpstreamFailed("telegram", detail) after its own retries; the detail never contains the
    bot token.

    Moving to WhatsApp (FR-036, blueprint F-10): a WhatsApp Cloud API sender implementing these
    two methods replaces `TelegramNotifier` in `app.core.deps.get_notifier`; finalise, the
    summary state machine and the alerts do not change. Outside WhatsApp's 24-hour window the
    summary must be an approved template (headline and counts plus the SCR-04 link), so that
    sender would build its own short text from the same `SummaryMessage`."""

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
        self.on_retry = on_retry
        """Hears each retry (`send_summary` points it at the `notify` step's `RETRY` events)."""

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
        secret = "" if token is None else token.get_secret_value()
        if not secret and self._fault is not Fault.TELEGRAM_DOWN:
            raise UpstreamFailed("telegram", "bot token not set")
        url = f"{TELEGRAM_API}/bot{secret}/{method}"
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
        if self.on_retry is not None:
            self.on_retry(detail)

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


# --- the controller summary (F-10.R8; contracts/telegram-message.md, data-model.md §4) -----

MAX_LINES = 20
MAX_CHARS = 4096
"""The message is at most 20 lines and under 4,096 characters (F-10.R8)."""
NUMBERED_MAX = 8
SECTION_TITLES = {1: "ACT ON THESE", 2: "SOURCES DISAGREE", 3: "WAITING ON REVIEW"}
REPORT_FILENAME = "report.html"
STALE_AFTER = timedelta(minutes=2)
"""A summary `SENDING` for longer than this is stale: never resent, ops alerted (F-10.R7)."""
STALE_GROUP = "stale"
"""`ALERT:{run_id}:notify:stale`: the one ops alert for a stale summary (research R12)."""
_MATCH = (Outcome.MATCHED, Outcome.MATCHED_AFTER_CONVERSION)


class MessageTooLong(Exception):
    """A rendered summary over the F-10.R8 limits: a template bug, never truncated silently."""


@dataclass(frozen=True)
class MessageSection:
    """One upper-case section with its numbered items `(number, text)`."""

    title: str
    items: list[tuple[int, str]]


@dataclass(frozen=True)
class SummaryMessage:
    """data-model.md §4; `render` gives the plain text (no parse mode)."""

    headline: str
    sections: list[MessageSection]
    more: int
    also: str | None
    proof_line: str
    link: str

    def render(self) -> str:
        """The contracts/telegram-message.md layout; MessageTooLong over 20 lines or at or
        over 4,096 characters."""
        lines = [self.headline, ""]
        for section in self.sections:
            lines += [section.title, *(f"{n}. {text}" for n, text in section.items), ""]
        if not self.sections:
            lines += ["No exceptions.", ""]
        if self.more:
            lines.append(f"+{self.more} more")
        if self.also:
            lines.append(self.also)
        lines += [self.proof_line, self.link]
        text = "\n".join(lines)
        if len(lines) > MAX_LINES or len(text) >= MAX_CHARS:
            raise MessageTooLong(f"{len(lines)} lines, {len(text)} characters")
        return text


def build_summary_message(view: RunView, app_base_url: str, proof: Proof) -> SummaryMessage:
    """The summary from the exception list only (F-09.R1, F-10.R8, C4): categories 1 to 3
    numbered (at most 8, then `+N more`), categories 4 and 5 in the "Also:" line, the proof
    line from proof C2 and the link to the run overview (SCR-04)."""
    groups = sum(n for o, n in view.counts.items() if o not in ROW_ONLY_OUTCOMES)
    matched = sum(view.counts.get(o, 0) for o in _MATCH)
    headline = (
        f"{report.month_label(view.month)} receiving: {groups} GRNs, {matched} match, "
        f"{groups - matched} need attention"
    )
    numbered = [e for e in view.exceptions if e.category in SECTION_TITLES]
    sections: list[MessageSection] = []
    for number, item in enumerate(numbered[:NUMBERED_MAX], start=1):
        title = SECTION_TITLES[item.category]
        if not sections or sections[-1].title != title:
            sections.append(MessageSection(title=title, items=[]))
        sections[-1].items.append((number, item.text))
    c2 = proof.checks.C2
    return SummaryMessage(
        headline=headline,
        sections=sections,
        more=max(0, len(numbered) - NUMBERED_MAX),
        also=report.also_line(view.exceptions),
        proof_line=f"Proof: {c2.rows - len(c2.unplaced)} of {c2.rows} rows placed once.",
        link=f"Report: {app_base_url.rstrip('/')}/runs/{view.id}",
    )


# --- sending it at most once (F-10.R4 to R7; data-model.md §6, research R12) --------------


@contextmanager
def _retry_events(notifier: Notifier, step: StepLog) -> Iterator[None]:
    """While sending, each HTTP retry of the real notifier writes a `RETRY` run event (FR-037).
    Test fakes have no retries of their own."""
    if not isinstance(notifier, TelegramNotifier):
        yield
        return
    previous, notifier.on_retry = notifier.on_retry, step.retry
    try:
        yield
    finally:
        notifier.on_retry = previous


class _NotifierFailed(LedgerlineError):
    """Wraps a notifier error so it is told apart from a repo error after a send; its text (the
    `notify FAILED` event detail) is the coded error's own, else the type name only."""

    def __init__(self, error: Exception) -> None:
        coded = isinstance(error, LedgerlineError)
        super().__init__(str(error) if coded else type(error).__name__)
        self.error = error


def send_summary(
    repo: Repo,
    notifier: Notifier,
    run: Run,
    notification: Notification,
    text: str,
    report_bytes: bytes,
    *,
    settings: Settings | None = None,
    now: Callable[[], datetime] = clock.now,
) -> Notification:
    """One send pass (research R12): CAS `PENDING`/`FAILED → SENDING` (attempts + 1; a lost
    race is StateConflict); the message unless `telegram_message_id` is already set, its id
    stored at once; then `report.html`; `SENT` with `sent_at` and audit `SUMMARY_SENT`. A
    notifier failure (after its own retries) sets `FAILED` with `last_error`, audits
    `SUMMARY_FAILED` and alerts ops; it is returned, not raised. A repo failure after a send
    propagates and leaves the row `SENDING`, which the stale check then surfaces (a duplicate
    is worse than a delay). Run events: `notify` STARTED, RETRY per HTTP retry, OK or FAILED."""
    settings = settings or get_settings()
    n = repo.update_notification(
        notification.id,
        expected={NotificationStatus.PENDING, NotificationStatus.FAILED},
        status=NotificationStatus.SENDING,
        attempts=notification.attempts + 1,
        updated_at=now(),
    )
    detail = {"notification_id": str(n.id)}
    try:
        with stage_step(repo, run.id, Stage.NOTIFY, detail, now=now) as step:
            with _retry_events(notifier, step):
                n = _deliver(repo, notifier, n, text, report_bytes, settings, now)
    except _NotifierFailed as failed:
        return _summary_failed(repo, notifier, run, n.id, failed.error, settings, now)
    done = now()
    n = repo.update_notification(
        n.id,
        expected={NotificationStatus.SENDING},
        status=NotificationStatus.SENT,
        sent_at=done,
        updated_at=done,
    )
    audit(
        repo,
        "system",
        AuditAction.SUMMARY_SENT,
        "notification",
        str(n.id),
        run_id=run.id,
        before={"status": NotificationStatus.SENDING.value},
        after={
            "status": n.status.value,
            "telegram_message_id": n.telegram_message_id,
            "sent_at": done.isoformat(),
        },
        channel=AuditChannel.TELEGRAM,
        now=lambda: done,
    )
    return n


def _deliver(
    repo: Repo,
    notifier: Notifier,
    n: Notification,
    text: str,
    report_bytes: bytes,
    settings: Settings,
    now: Callable[[], datetime],
) -> Notification:
    """Message (unless already delivered; its id stored before anything else), then document.
    Notifier errors come out as `_NotifierFailed`."""
    chat_id = settings.TELEGRAM_CONTROLLER_CHAT_ID
    if not chat_id:
        raise _NotifierFailed(UpstreamFailed("telegram", "controller chat not set"))
    if n.telegram_message_id is None:
        try:
            message_id = notifier.send_message(chat_id, text)
        except Exception as exc:
            raise _NotifierFailed(exc) from exc
        n = repo.update_notification(
            n.id,
            expected={NotificationStatus.SENDING},
            telegram_message_id=message_id,
            updated_at=now(),
        )
    try:
        notifier.send_document(chat_id, REPORT_FILENAME, report_bytes)
    except Exception as exc:
        raise _NotifierFailed(exc) from exc
    return n


def _summary_failed(
    repo: Repo,
    notifier: Notifier,
    run: Run,
    notification_id: UUID,
    exc: Exception,
    settings: Settings,
    now: Callable[[], datetime],
) -> Notification:
    """`FAILED` + `last_error` (code and token-free reason), audit `SUMMARY_FAILED`, one ops
    alert (F-10.R6, FM-11, FR-035). The controller is never told about failures."""
    from app.services.alerts import error_parts, stage_failed_alert  # alerts imports Notifier

    code, reason = error_parts(exc)
    at = now()
    n = repo.update_notification(
        notification_id,
        expected={NotificationStatus.SENDING},
        status=NotificationStatus.FAILED,
        last_error=f"{code}: {reason}",
        updated_at=at,
    )
    audit(
        repo,
        "system",
        AuditAction.SUMMARY_FAILED,
        "notification",
        str(n.id),
        run_id=run.id,
        before={"status": NotificationStatus.SENDING.value},
        after={"status": n.status.value, "last_error": n.last_error},
        channel=AuditChannel.TELEGRAM,
        now=lambda: at,
    )
    stage_failed_alert(repo, notifier, run, Stage.NOTIFY, exc, settings=settings, now=now)
    return n


def summary_of(repo: Repo, run: Run) -> Notification | None:
    """This run's `SUMMARY:{run_key}` notification. C7: an unfinished run may share its key
    with another run, so a row of another run is not this run's summary."""
    if run.run_key is None:
        return None
    n = repo.get_notification_by_key(summary_key(run.run_key))
    return n if n is not None and n.run_id == run.id else None


def is_stale(n: Notification, at: datetime) -> bool:
    """`SENDING` for over `STALE_AFTER` (scenario clock)."""
    return n.status is NotificationStatus.SENDING and n.updated_at < at - STALE_AFTER


def check_stale_summary(
    repo: Repo,
    notifier: Notifier,
    run_id: UUID,
    *,
    settings: Settings | None = None,
    now: Callable[[], datetime] = clock.now,
) -> Notification | None:
    """F-10.R7, FM-12, C22: the run's summary when it is stale, after one ops alert ever
    (`ALERT:{run_id}:notify:stale`; repeats find the alert row and send nothing); None
    otherwise. Never resends. Called on every EP-17 call and EP-05 read of a finalised run."""
    from app.services.alerts import stage_failed_alert  # alerts imports Notifier

    run = repo.get_run(run_id)
    n = summary_of(repo, run)
    if n is None or not is_stale(n, now()):
        return None
    error = ApiError(
        ErrorCode.RUN_STATE_CONFLICT,
        409,
        "summary stuck sending for over 2 minutes; check Telegram by hand, do not resend",
        Stage.NOTIFY,
    )
    stage_failed_alert(
        repo,
        notifier,
        run,
        Stage.NOTIFY,
        error,
        settings=settings,
        now=now,
        attempt_group=STALE_GROUP,
    )
    return n
