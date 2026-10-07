"""Run log (F-11.R2) and the one retry policy (research R8).

`stage_step` writes `STARTED`, then `OK` or `FAILED` with `duration_ms`, and `RETRY` events
through `step.retry`. `retrying` retries transient errors only (timeouts, connection errors,
429, 5xx); a 4xx is rejected at once. Both end in `UpstreamFailed(service, detail)`; any other
error (a bug) passes through unchanged. Details never quote URLs (Telegram URLs hold the token).
"""

import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, TypeVar
from uuid import UUID, uuid4

import httpx
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_chain,
    wait_fixed,
    wait_none,
)

from app.core import clock
from app.core.db import Repo
from app.core.errors import UpstreamFailed
from app.domain.enums import RunEventStatus, Stage
from app.domain.models import RunEvent

T = TypeVar("T")

OnRetry = Callable[[dict[str, Any]], None]
"""Hears `{"service", "attempt", "cause"}` before each wait; `attempt` is the next one."""


@dataclass
class StepLog:
    """The open step of `stage_step`; `retry` writes one `RETRY` event."""

    repo: Repo
    run_id: UUID
    stage: Stage
    now: Callable[[], datetime]

    def write(
        self, status: RunEventStatus, detail: dict[str, Any], duration_ms: int | None = None
    ) -> datetime:
        at = self.now()
        self.repo.add_run_event(
            RunEvent(
                id=uuid4(),
                run_id=self.run_id,
                stage=self.stage,
                status=status,
                at=at,
                duration_ms=duration_ms,
                detail=detail,
            )
        )
        return at

    def retry(self, detail: dict[str, Any]) -> None:
        self.write(RunEventStatus.RETRY, dict(detail))


@contextmanager
def stage_step(
    repo: Repo,
    run_id: UUID,
    stage: Stage,
    detail: dict[str, Any] | None = None,
    page_no: int | None = None,
    *,
    now: Callable[[], datetime] = clock.now,
) -> Iterator[StepLog]:
    """F-11.R2: `STARTED` on entry; `OK` with the same detail, or `FAILED` with
    `{"error": <message>}` (then re-raised), each with `duration_ms` since `STARTED`."""
    base = dict(detail or {})
    if page_no is not None:
        base["page_no"] = page_no
    step = StepLog(repo, run_id, stage, now)
    started = step.write(RunEventStatus.STARTED, base)

    def elapsed_ms() -> int:
        return (now() - started) // timedelta(milliseconds=1)

    try:
        yield step
    except BaseException as exc:
        step.write(RunEventStatus.FAILED, {"error": _error_text(exc)}, elapsed_ms())
        raise
    step.write(RunEventStatus.OK, base, elapsed_ms())


def _error_text(exc: BaseException) -> str:
    """The FAILED detail; an httpx error is named by type and status only (its text has the URL)."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"{type(exc).__name__}: HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        return type(exc).__name__
    return str(exc) or type(exc).__name__


def transient_cause(error: BaseException | None) -> str | None:
    """The cause of a retryable failure, or None when it must not be retried (R8)."""
    if isinstance(error, httpx.TimeoutException):  # before its base, TransportError
        return "timeout"
    if isinstance(error, httpx.TransportError):
        return "connection error"
    if isinstance(error, httpx.HTTPStatusError):
        code = error.response.status_code
        if code == 429:
            return "rate limited"
        if code >= 500:
            return "server error"
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, ConnectionError):
        return "connection error"
    return None


def is_transient(error: BaseException) -> bool:
    """Default `retry_if`: transient errors retry; 4xx (other than 429) and bugs do not."""
    return transient_cause(error) is not None


def _rejected(error: BaseException) -> str | None:
    """`rejected: HTTP <code>` for a non-retryable 4xx; never the URL."""
    if isinstance(error, httpx.HTTPStatusError) and 400 <= error.response.status_code < 500:
        return f"rejected: HTTP {error.response.status_code}"
    return None


@dataclass(frozen=True)
class Retrier:
    """Built by `retrying`; call it with the zero-argument operation."""

    service: str
    attempts: int
    waits: Sequence[float]
    on_retry: OnRetry | None
    retry_if: Callable[[BaseException], bool]
    sleep: Callable[[float], None]

    def __call__(self, fn: Callable[[], T]) -> T:
        def before_sleep(state: RetryCallState) -> None:
            if self.on_retry is None:
                return
            error = state.outcome.exception() if state.outcome is not None else None
            cause = transient_cause(error) or type(error).__name__
            self.on_retry(
                {"service": self.service, "attempt": state.attempt_number + 1, "cause": cause}
            )

        policy = Retrying(
            stop=stop_after_attempt(self.attempts),
            wait=wait_chain(*(wait_fixed(s) for s in self.waits)) if self.waits else wait_none(),
            retry=retry_if_exception(self.retry_if),
            before_sleep=before_sleep,
            sleep=self.sleep,
            reraise=True,
        )
        try:
            return policy(fn)
        except UpstreamFailed:
            raise
        except Exception as exc:
            rejected = _rejected(exc)
            if rejected is not None:
                raise UpstreamFailed(self.service, rejected) from exc
            if not self.retry_if(exc):
                raise
            cause = transient_cause(exc) or type(exc).__name__
            detail = f"{cause} after {self.attempts} attempts"
            raise UpstreamFailed(self.service, detail) from exc


def retrying(
    service: str,
    attempts: int,
    waits: Sequence[float],
    on_retry: OnRetry | None = None,
    *,
    retry_if: Callable[[BaseException], bool] = is_transient,
    sleep: Callable[[float], None] = time.sleep,
) -> Retrier:
    """R8: up to `attempts` calls, waiting `waits[i]` before call i+2 (the last wait repeats);
    `UpstreamFailed(service, "<cause> after N attempts" | "rejected: HTTP <code>")` at the end.
    Tests pass `sleep` so nothing waits."""
    return Retrier(service, attempts, tuple(waits), on_retry, retry_if, sleep)
