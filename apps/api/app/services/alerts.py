"""Ops alert when a stage ends FAILED (F-11.R4, F-11.R5, contracts/alerts.md, research R9).

Only the ops chat is ever messaged; the controller chat is not read here. The alert never
raises: a delivery failure is recorded on the notification and the run instead (F-11.R5).
"""

import logging
from collections.abc import Callable
from datetime import datetime
from uuid import UUID, uuid4

from app.core import clock
from app.core.config import Settings, get_settings
from app.core.db import Repo
from app.core.errors import LedgerlineError, UpstreamFailed
from app.domain.enums import (
    AuditAction,
    AuditChannel,
    ErrorCode,
    NotificationKind,
    NotificationStatus,
    Stage,
)
from app.domain.models import AuditEntry, Notification, Run
from app.services.telegram import Notifier

log = logging.getLogger(__name__)

MAX_TEXT = 4096
"""Telegram's message length limit."""
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_SERVICES = {"openai": "OpenAI", "telegram": "Telegram", "supabase": "Supabase"}


def alert_key(run_id: UUID, stage: Stage, at: datetime) -> str:
    """`ALERT:{run_id}:{stage}:{YYYYMMDDHHMM}`: one alert per stage per scenario minute."""
    return f"ALERT:{run_id}:{stage.value}:{at.strftime('%Y%m%d%H%M')}"


def error_parts(error: BaseException) -> tuple[str, str]:
    """`(code, short reason)` for the "Error:" line. A coded error (`code: ErrorCode` and
    `message`, e.g. ApiError) gives its own; anything else gives only its type name, never its
    text, so no file contents or keys reach the chat."""
    if isinstance(error, UpstreamFailed):
        service = _SERVICES.get(error.service, error.service)
        return ErrorCode.UPSTREAM_FAILED.value, f"{service} {error.detail}"
    code = getattr(error, "code", None)
    if isinstance(code, ErrorCode):
        return code.value, str(getattr(error, "message", "") or "")
    return ErrorCode.INTERNAL_ERROR.value, type(error).__name__


def alert_text(
    run: Run, stage: Stage, error: BaseException, page_no: int | None, app_base_url: str
) -> str:
    """The plain-text alert of contracts/alerts.md, cut to 4,096 characters (link kept)."""
    year, month = run.month.split("-")
    if page_no is None:
        page_no = getattr(error, "page_no", None)
    page = f" (page {page_no})" if stage is Stage.EXTRACT and page_no is not None else ""
    code, reason = error_parts(error)
    head = f"LEDGERLINE FAILED · {_MONTHS[int(month) - 1]} {year} · run {str(run.id)[:4]}…"
    tail = f"Controller was NOT messaged. Retry from {app_base_url.rstrip('/')}/runs/{run.id}"
    error_line = f"Error: {code}: {reason}"
    room = MAX_TEXT - len(head) - len(f"\nStage: {stage.value}{page}\n") - len(tail) - 1
    if len(error_line) > room:
        error_line = error_line[: room - 1] + "…"
    return f"{head}\nStage: {stage.value}{page}\n{error_line}\n{tail}"


def stage_failed_alert(
    repo: Repo,
    notifier: Notifier,
    run: Run,
    stage: Stage,
    error: BaseException,
    page_no: int | None = None,
    *,
    settings: Settings | None = None,
    now: Callable[[], datetime] = clock.now,
) -> Notification:
    """Send one ops alert for a stage that ended FAILED after its retries (contracts/alerts.md
    steps 1 to 5). A repeat in the same scenario minute returns the existing notification
    without sending. Never raises for a delivery failure."""
    settings = settings or get_settings()
    at = now()
    pending = Notification(
        id=uuid4(),
        run_id=run.id,
        kind=NotificationKind.ALERT,
        idempotency_key=alert_key(run.id, stage, at),
        status=NotificationStatus.PENDING,
        attempts=0,
        last_error=None,
        telegram_message_id=None,
        sent_at=None,
        updated_at=at,
    )
    n, created = repo.insert_notification(pending)
    if not created:
        return n
    n = repo.update_notification(
        n.id,
        expected={NotificationStatus.PENDING},
        status=NotificationStatus.SENDING,
        attempts=n.attempts + 1,
        updated_at=at,
    )
    try:
        chat_id = settings.TELEGRAM_OPS_CHAT_ID
        if not chat_id:
            raise UpstreamFailed("telegram", "ops chat not set")
        text = alert_text(run, stage, error, page_no, settings.APP_BASE_URL)
        message_id = notifier.send_message(chat_id, text)
    except Exception as exc:  # F-11.R5: a failed alert must not mask the stage's own error
        return _record_failure(repo, n, exc, now())
    done = now()
    n = repo.update_notification(
        n.id,
        expected={NotificationStatus.SENDING},
        status=NotificationStatus.SENT,
        telegram_message_id=message_id,
        sent_at=done,
        updated_at=done,
    )
    repo.append_audit(
        AuditEntry(
            id=0,  # assigned by the repo
            at=done,
            actor="system",
            action=AuditAction.ALERT_SENT,
            entity="notification",
            entity_id=str(n.id),
            run_id=run.id,
            before=None,
            after={"stage": stage.value, "status": n.status.value},
            channel=AuditChannel.TELEGRAM,
        )
    )
    return n


def _record_failure(repo: Repo, n: Notification, exc: Exception, at: datetime) -> Notification:
    """FAILED with a token-free `last_error`; the run's error_message notes it (F-11.R5)."""
    if isinstance(exc, UpstreamFailed):
        code, last_error = ErrorCode.UPSTREAM_FAILED.value, f"UPSTREAM_FAILED: {exc.detail}"
    else:
        code = ErrorCode.INTERNAL_ERROR.value
        last_error = f"{code}: {type(exc).__name__}"
    log.warning("ops alert not delivered", extra={"run_id": str(n.run_id), "error_code": code})
    n = repo.update_notification(
        n.id,
        expected={NotificationStatus.SENDING},
        status=NotificationStatus.FAILED,
        last_error=last_error,
        updated_at=at,
    )
    note = f"ops alert not delivered ({code})"
    try:
        current = repo.get_run(n.run_id).error_message
        repo.update_run_fields(n.run_id, error_message=f"{current}; {note}" if current else note)
    except LedgerlineError:  # e.g. the run was finalised meanwhile; the notification records it
        log.warning("alert failure not recorded on run", extra={"run_id": str(n.run_id)})
    return n
