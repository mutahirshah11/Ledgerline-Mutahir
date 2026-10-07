"""Telegram sender (contracts/alerts.md; F-10.R5, F-11.R4, C21, research R3/R7/R8) and the
controller summary (F-10.R4 to R8; 005 contracts/telegram-message.md, data-model.md §4, §6).

The bot token is part of the request URL, so no URL and no httpx error text ever leaves this
module: errors carry only a cause word, and httpx's own request log line is redacted here.
"""

import logging
import mimetypes
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol, Self
from uuid import UUID

import httpx

from app.core import clock
from app.core.config import Settings, get_settings
from app.core.db import Repo
from app.core.errors import ApiError, LedgerlineError, StateConflict, UpstreamFailed
from app.domain.enums import (
    AuditAction,
    AuditChannel,
    ErrorCode,
    Fault,
    NotificationStatus,
    Outcome,
    RunEventStatus,
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

    def send_message(self, chat_id: str, text: str, *, on_retry: OnRetry | None = None) -> int:
        """`on_retry` hears each of this call's own retries (the `notify` RETRY run events)."""
        ...

    def send_document(
        self,
        chat_id: str,
        filename: str,
        content: bytes,
        caption: str | None = None,
        *,
        on_retry: OnRetry | None = None,
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


class MaybeDelivered(UpstreamFailed):
    """C1 (research R11, R12): a `sendMessage` failure after the request may have reached
    Telegram (read/write timeout, dropped connection, 5xx, an unreadable 200). Never retried:
    a duplicate summary is worse than a delay (F-10.R7)."""


_NOT_SENT = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.UnsupportedProtocol,
    httpx.LocalProtocolError,
)
"""Transport errors raised before any byte of the request left: proved not delivered."""


def _cause_of(error: BaseException) -> str | None:
    """Only `_Retryable` retries; `_post` already turned every httpx error into a cause word."""
    return error.cause if isinstance(error, _Retryable) else None


def _retry_heard(detail: dict[str, Any], on_retry: OnRetry | None) -> None:
    log.warning("telegram send failed; retrying", extra=detail)
    if on_retry is not None:
        on_retry(detail)


class TelegramNotifier:
    """`sendMessage` as plain text (no `parse_mode`) and `sendDocument` (multipart), each with
    `TELEGRAM_ATTEMPTS` attempts, the `TELEGRAM_BACKOFF_S` waits and `TELEGRAM_TIMEOUT_S` per
    call. 4xx answers are not retried. C1: `sendMessage` retries only failures that prove
    nothing was delivered (connection refused or not made, 429); any other failure raises
    `MaybeDelivered` at once. `sendDocument` retries those too (a duplicate document is
    acceptable). `fault=TELEGRAM_DOWN` fails every attempt without a network call, as a refused
    connection would (Section 25)."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        fault: Fault | None = None,
    ) -> None:
        _install_log_filter()
        self._settings = settings or get_settings()
        self._client = client
        """Built on the first call (M3: EP-05 gets a notifier per request and rarely sends)."""
        self._sleep = sleep
        self._fault = fault

    def close(self) -> None:
        """Close the HTTP client (also an injected one), if any."""
        if self._client is not None:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def send_message(self, chat_id: str, text: str, *, on_retry: OnRetry | None = None) -> int:
        """Plain text, link previews off (research R11: the `Report:` link stays a line)."""
        body = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        return self._send("sendMessage", {"json": body}, on_retry, once=True)

    def send_document(
        self,
        chat_id: str,
        filename: str,
        content: bytes,
        caption: str | None = None,
        *,
        on_retry: OnRetry | None = None,
    ) -> int:
        """`sendDocument` as multipart (005 research R11; F-10 sends `report.html`), content
        type from the filename. A caption over `CAPTION_MAX` is a caller bug: ValueError before
        any call, never a retry."""
        if caption is not None and len(caption) > CAPTION_MAX:
            raise ValueError(f"caption is {len(caption)} characters; Telegram allows {CAPTION_MAX}")
        data = {"chat_id": chat_id} | ({} if caption is None else {"caption": caption})
        mime = _MIME.guess_type(filename)[0] or "application/octet-stream"
        files = {"document": (filename, content, mime)}
        return self._send("sendDocument", {"data": data, "files": files}, on_retry, once=False)

    def _send(
        self, method: str, body: dict[str, Any], on_retry: OnRetry | None, *, once: bool
    ) -> int:
        """One Bot API call with the retry policy; `body` is httpx's `post` keyword args;
        `once`: a possibly delivered failure raises `MaybeDelivered` instead of retrying."""
        token = self._settings.TELEGRAM_BOT_TOKEN
        secret = "" if token is None else token.get_secret_value()
        if not secret and self._fault is not Fault.TELEGRAM_DOWN:
            raise UpstreamFailed("telegram", "bot token not set")
        url = f"{TELEGRAM_API}/bot{secret}/{method}"
        policy = retrying(
            "telegram",
            max(1, self._settings.TELEGRAM_ATTEMPTS),
            self._settings.TELEGRAM_BACKOFF_S,
            lambda detail: _retry_heard(detail, on_retry),
            sleep=self._sleep,
            chain=False,
            cause_of=_cause_of,
        )
        return policy(lambda: self._post(url, body, once))

    def _post(self, url: str, body: dict[str, Any], once: bool) -> int:
        """One call: the message id, `_Retryable`, `MaybeDelivered` (`once`) or UpstreamFailed
        for a final rejection. Raised errors never chain the httpx exception (its text can hold
        the URL)."""

        def unsure(cause: str) -> Exception:
            if once:
                return MaybeDelivered("telegram", f"{cause}, maybe delivered")
            return _Retryable(cause)

        if self._fault is Fault.TELEGRAM_DOWN:
            raise _Retryable("down")
        if self._client is None:
            self._client = httpx.Client()
        try:
            response = self._client.post(url, timeout=self._settings.TELEGRAM_TIMEOUT_S, **body)
        except _NOT_SENT as exc:
            cause = "timeout" if isinstance(exc, httpx.TimeoutException) else "connection error"
            raise _Retryable(cause) from None
        except httpx.TimeoutException:
            raise unsure("timeout") from None
        except httpx.TransportError:
            raise unsure("connection error") from None
        status = response.status_code
        if status == 429:
            raise _Retryable("rate limited")
        if status >= 500:
            raise unsure("server error")
        if status >= 400:
            raise UpstreamFailed("telegram", f"rejected: HTTP {status}")
        try:
            answer = response.json()
        except ValueError:  # a 200 Telegram may have acted on
            if once:
                raise MaybeDelivered("telegram", "unexpected answer, maybe delivered") from None
            raise UpstreamFailed("telegram", "rejected: unexpected answer") from None
        result = answer.get("result") if isinstance(answer, dict) else None
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if not isinstance(answer, dict) or answer.get("ok") is not True:
            raise UpstreamFailed("telegram", "rejected: unexpected answer")
        if not isinstance(message_id, int):
            if once:
                raise MaybeDelivered("telegram", "unexpected answer, maybe delivered")
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
ITEM_MAX = 200
ALSO_MAX = 600
"""C2 caps (contracts/telegram-message.md "Caps"): a numbered item's text and the whole Also
line end in "…" past these lengths, so 8 items of any length still fit F-10.R8."""
_NEWLINE = re.compile(r"\r\n|[\r\n]")


def _one_line(text: str, limit: int) -> str:
    """Newlines become spaces (a note can hold them); over `limit` it is cut to end in "…"."""
    flat = _NEWLINE.sub(" ", text)
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


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
        """The contracts/telegram-message.md layout; MessageTooLong over 20 lines (newlines
        counted) or at or over 4,096 characters."""
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
        count = text.count("\n") + 1
        if count > MAX_LINES or len(text) >= MAX_CHARS:
            raise MessageTooLong(f"{count} lines, {len(text)} characters")
        return text


def build_summary_message(view: RunView, app_base_url: str, proof: Proof) -> SummaryMessage:
    """The summary from the exception list only (F-09.R1, F-10.R8, C4): categories 1 to 3
    numbered (at most 8, then `+N more`), categories 4 and 5 in the "Also:" line, the proof
    line from proof C2 and the link to the run overview (SCR-04). Item texts and the Also
    line are one line each, capped at `ITEM_MAX` / `ALSO_MAX` (C2)."""
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
        sections[-1].items.append((number, _one_line(item.text, ITEM_MAX)))
    also = report.also_line(view.exceptions)
    c2 = proof.checks.C2
    return SummaryMessage(
        headline=headline,
        sections=sections,
        more=max(0, len(numbered) - NUMBERED_MAX),
        also=None if also is None else _one_line(also, ALSO_MAX),
        proof_line=f"Proof: {c2.rows - len(c2.unplaced)} of {c2.rows} rows placed once.",
        link=f"Report: {app_base_url.rstrip('/')}/runs/{view.id}",
    )


# --- sending it at most once (F-10.R4 to R7; data-model.md §6, research R12) --------------


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
    race is 409 "The summary send is in progress", I3); the message unless
    `telegram_message_id` is already set, its id stored at once; then `report.html`; `SENT`
    with `sent_at` and audit `SUMMARY_SENT`. A notifier failure (after its own retries) sets
    `FAILED` with `last_error`, audits `SUMMARY_FAILED` and alerts ops; it is returned, not
    raised. A message that may have reached Telegram (`MaybeDelivered`, C1) and a repo failure
    after a send both leave the row `SENDING`, never resent (a duplicate is worse than a
    delay): the first alerts ops at once under the stale key, the second propagates and the
    stale check surfaces it. Run events: `notify` STARTED, RETRY per HTTP retry, OK or FAILED."""
    settings = settings or get_settings()
    try:
        n = repo.update_notification(
            notification.id,
            expected={NotificationStatus.PENDING, NotificationStatus.FAILED},
            status=NotificationStatus.SENDING,
            attempts=notification.attempts + 1,
            updated_at=now(),
        )
    except StateConflict as exc:  # another pass holds the row (I3)
        raise ApiError(
            ErrorCode.RUN_STATE_CONFLICT, 409, "The summary send is in progress", Stage.NOTIFY
        ) from exc
    detail = {"notification_id": str(n.id)}
    try:
        with stage_step(repo, run.id, Stage.NOTIFY, detail, now=now) as step:
            n = _deliver(repo, notifier, n, text, report_bytes, settings, now, step)
    except MaybeDelivered as unsure:
        return _maybe_delivered(repo, notifier, run, n, unsure, settings, now)
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
    step: StepLog,
) -> Notification:
    """Message (unless already delivered; its id stored before anything else), then document;
    each HTTP retry is a `RETRY` run event (FR-037). Notifier errors come out as
    `_NotifierFailed`."""
    chat_id = settings.TELEGRAM_CONTROLLER_CHAT_ID
    if not chat_id:
        raise _NotifierFailed(UpstreamFailed("telegram", "controller chat not set"))
    if n.telegram_message_id is None:
        try:
            message_id = notifier.send_message(chat_id, text, on_retry=step.retry)
        except MaybeDelivered:
            raise
        except Exception as exc:
            raise _NotifierFailed(exc) from exc
        n = repo.update_notification(
            n.id,
            expected={NotificationStatus.SENDING},
            telegram_message_id=message_id,
            updated_at=now(),
        )
    try:
        notifier.send_document(chat_id, REPORT_FILENAME, report_bytes, on_retry=step.retry)
    except Exception as exc:
        raise _NotifierFailed(exc) from exc
    return n


def summary_not_built(
    repo: Repo,
    notifier: Notifier,
    run: Run,
    notification: Notification,
    exc: Exception,
    *,
    settings: Settings | None = None,
    now: Callable[[], datetime] = clock.now,
) -> Notification:
    """C2: the summary text of a resend could not be built or rendered. Nothing is sent: the
    row (`PENDING`/`FAILED`) becomes `FAILED` with `SUMMARY_FAILED` and one ops alert, as a
    failed send would; run event `notify` FAILED. A lost CAS is 409 (I3)."""
    from app.services.alerts import error_parts

    settings = settings or get_settings()
    code, reason = error_parts(exc)
    detail = {"notification_id": str(notification.id), "error": f"{code}: {reason}"}
    StepLog(repo, run.id, Stage.NOTIFY, now).write(RunEventStatus.FAILED, detail)
    expected = {NotificationStatus.PENDING, NotificationStatus.FAILED}
    try:
        return _summary_failed(
            repo, notifier, run, notification.id, exc, settings, now, expected, notification.status
        )
    except StateConflict as conflict:
        raise ApiError(
            ErrorCode.RUN_STATE_CONFLICT, 409, "The summary send is in progress", Stage.NOTIFY
        ) from conflict


def _summary_failed(
    repo: Repo,
    notifier: Notifier,
    run: Run,
    notification_id: UUID,
    exc: Exception,
    settings: Settings,
    now: Callable[[], datetime],
    expected: set[NotificationStatus] | None = None,
    before: NotificationStatus = NotificationStatus.SENDING,
) -> Notification:
    """`FAILED` + `last_error` (code and token-free reason), audit `SUMMARY_FAILED`, one ops
    alert (F-10.R6, FM-11, FR-035). The controller is never told about failures."""
    from app.services.alerts import error_parts, stage_failed_alert  # alerts imports Notifier

    code, reason = error_parts(exc)
    at = now()
    n = repo.update_notification(
        notification_id,
        expected=expected or {NotificationStatus.SENDING},
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
        before={"status": before.value},
        after={"status": n.status.value, "last_error": n.last_error},
        channel=AuditChannel.TELEGRAM,
        now=lambda: at,
    )
    stage_failed_alert(repo, notifier, run, Stage.NOTIFY, exc, settings=settings, now=now)
    return n


def _maybe_delivered(
    repo: Repo,
    notifier: Notifier,
    run: Run,
    n: Notification,
    exc: MaybeDelivered,
    settings: Settings,
    now: Callable[[], datetime],
) -> Notification:
    """C1: the row stays `SENDING` (no `FAILED`, so no resend); ops are alerted once under the
    stale key, so the later stale check finds that alert and sends no second one (F-10.R7)."""
    from app.services.alerts import stage_failed_alert  # alerts imports Notifier

    log.warning("summary maybe delivered; left SENDING", extra={"run_id": str(run.id)})
    error = ApiError(
        ErrorCode.RUN_STATE_CONFLICT,
        409,
        f"summary may have reached Telegram ({exc.detail}); check Telegram by hand, do not resend",
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
