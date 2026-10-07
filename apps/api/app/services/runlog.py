"""Run log (F-11.R2) and the one retry policy (research R8).

`stage_step` writes `STARTED`, then `OK` or `FAILED` with `duration_ms`, and `RETRY` events
through `step.retry`; every event keeps the step detail (and `page_no`). `retrying` retries
transient errors only (timeouts, connection errors, 429, 5xx, invalid model output); a 4xx is
rejected at once. Both end in `UpstreamFailed(service, detail, attempts=n)`; an `UpstreamFailed`
from the operation passes through un-retried (an unset `attempts` filled in) and any other
error (a bug) unchanged. Details
never quote URLs (Telegram URLs hold the token); `chain=False` also drops the original error.
"""

import logging
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, NoReturn, TypeVar
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
from app.core.errors import InvalidReadOutput, UpstreamFailed
from app.domain.enums import RunEventStatus, Stage
from app.domain.models import RunEvent

T = TypeVar("T")

logger = logging.getLogger(__name__)

OnRetry = Callable[[dict[str, Any]], None]
"""Hears `{"service", "attempt", "cause"}` before each wait; `attempt` is the next one."""
CauseOf = Callable[[BaseException], str | None]
"""The cause word of a retryable error, or None when it must not be retried."""


@dataclass
class StepLog:
    """The open step of `stage_step`; `retry` writes one `RETRY` event with the step detail."""

    repo: Repo
    run_id: UUID
    stage: Stage
    now: Callable[[], datetime]
    base: dict[str, Any] = field(default_factory=dict)

    def write(
        self, status: RunEventStatus, detail: dict[str, Any], duration_ms: int | None = None
    ) -> None:
        self.repo.add_run_event(
            RunEvent(
                id=uuid4(),
                run_id=self.run_id,
                stage=self.stage,
                status=status,
                at=self.now(),
                duration_ms=duration_ms,
                detail=detail,
            )
        )

    def retry(self, detail: dict[str, Any]) -> None:
        self.write(RunEventStatus.RETRY, {**self.base, **detail})


def _error_text(exc: BaseException) -> str:
    """The FAILED detail; an httpx error is named by type and status only (its text has the URL)."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"{type(exc).__name__}: HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        return type(exc).__name__
    return str(exc) or type(exc).__name__


@contextmanager
def stage_step(
    repo: Repo,
    run_id: UUID,
    stage: Stage,
    detail: dict[str, Any] | None = None,
    page_no: int | None = None,
    *,
    now: Callable[[], datetime] = clock.now,
    timer: Callable[[], float] = time.monotonic,
) -> Iterator[StepLog]:
    """F-11.R2: `STARTED` on entry; `OK`, or `FAILED` with `"error"` added (then re-raised),
    each with the step detail and `duration_ms` measured by `timer` (event times from `now`).
    A failure to write `FAILED` is logged and never hides the original error."""
    base = dict(detail or {})
    if page_no is not None:
        base["page_no"] = page_no
    step = StepLog(repo, run_id, stage, now, base)
    step.write(RunEventStatus.STARTED, dict(base))
    started = timer()

    def elapsed_ms() -> int:
        return round((timer() - started) * 1000)

    try:
        yield step
    except Exception as exc:
        try:
            step.write(RunEventStatus.FAILED, {**base, "error": _error_text(exc)}, elapsed_ms())
        except Exception as write_exc:
            # Type names only: a traceback would print the original error, which may quote a URL.
            logger.error(
                "run event FAILED not written for %s: %s",
                stage.value,
                type(write_exc).__name__,
            )
        raise
    step.write(RunEventStatus.OK, dict(base), elapsed_ms())


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
    if isinstance(error, InvalidReadOutput):
        return "invalid output"
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, ConnectionError):
        return "connection error"
    return None


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
    cause_of: CauseOf
    sleep: Callable[[float], None]
    chain: bool

    def _cause(self, error: BaseException) -> str | None:
        return None if isinstance(error, UpstreamFailed) else self.cause_of(error)

    def __call__(self, fn: Callable[[], T]) -> T:
        made = 0

        def attempt() -> T:
            nonlocal made
            made += 1
            return fn()

        def before_sleep(state: RetryCallState) -> None:
            if self.on_retry is None:
                return
            error = state.outcome.exception() if state.outcome is not None else None
            cause = (self._cause(error) if error is not None else None) or "unknown"
            self.on_retry(
                {"service": self.service, "attempt": state.attempt_number + 1, "cause": cause}
            )

        waits = [wait_fixed(s) for s in self.waits]
        policy = Retrying(
            stop=stop_after_attempt(self.attempts),
            wait=wait_chain(*waits) if waits else wait_none(),
            retry=retry_if_exception(lambda e: self._cause(e) is not None),
            before_sleep=before_sleep,
            sleep=self.sleep,
            reraise=True,
        )
        try:
            return policy(attempt)
        except UpstreamFailed as exc:
            if exc.attempts is None:
                exc.attempts = made
            raise
        except Exception as exc:
            detail = _rejected(exc)
            if detail is None:
                cause = self._cause(exc)
                if cause is None:
                    raise
                detail = f"{cause} after {made} attempts"
            self._raise(UpstreamFailed(self.service, detail, attempts=made), exc)

    def _raise(self, error: UpstreamFailed, cause: Exception) -> NoReturn:
        if self.chain:
            raise error from cause
        raise error from None


def retrying(
    service: str,
    attempts: int,
    waits: Sequence[float],
    on_retry: OnRetry | None = None,
    *,
    cause_of: CauseOf = transient_cause,
    sleep: Callable[[float], None] = time.sleep,
    chain: bool = True,
) -> Retrier:
    """R8: up to `attempts` calls, retrying iff `cause_of(error)` is not None, waiting
    `waits[i]` before call i+2 (the last wait repeats); then `UpstreamFailed(service,
    "<cause> after N attempts" | "rejected: HTTP <code>", attempts=N)`. `chain=False` raises it
    `from None` so no original error (and no URL in it) reaches a traceback. Tests pass `sleep`."""
    return Retrier(service, attempts, tuple(waits), on_retry, cause_of, sleep, chain)
