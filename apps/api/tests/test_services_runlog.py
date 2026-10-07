"""Run log helpers (F-11.R2, research R8): `stage_step` events and the `retrying` policy."""

from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest

from app.core.errors import UpstreamFailed
from app.core.memory_repo import MemoryRepo
from app.domain.enums import RunEventStatus, Stage
from app.domain.models import Run
from app.services.runlog import retrying, stage_step
from tests.factories import T0, make_run


@pytest.fixture
def run(memory_repo: MemoryRepo) -> Run:
    return memory_repo.create_run(make_run())


def ticking(step_ms: int) -> Callable[[], datetime]:
    """A clock that moves `step_ms` forward on every call."""
    times: Iterator[datetime] = (T0 + timedelta(milliseconds=step_ms * i) for i in range(1000))
    return lambda: next(times)


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.example.test/send")
    return httpx.HTTPStatusError("bad", request=request, response=httpx.Response(status))


def test_F11_R2_stage_step_writes_started_ok_with_duration(
    memory_repo: MemoryRepo, run: Run
) -> None:
    with stage_step(memory_repo, run.id, Stage.EXTRACT, {"pages": 2}, page_no=1, now=ticking(250)):
        pass
    ok, started = memory_repo.list_run_events(run.id)  # newest first
    assert (started.stage, started.status, started.duration_ms) == (
        Stage.EXTRACT,
        RunEventStatus.STARTED,
        None,
    )
    assert started.detail == {"pages": 2, "page_no": 1}
    assert started.at == T0
    assert (ok.stage, ok.status, ok.duration_ms) == (Stage.EXTRACT, RunEventStatus.OK, 250)
    assert ok.detail == {"pages": 2, "page_no": 1}


def test_F11_R2_failure_writes_failed(memory_repo: MemoryRepo, run: Run) -> None:
    with (
        pytest.raises(UpstreamFailed),
        stage_step(memory_repo, run.id, Stage.NOTIFY, now=ticking(10)),
    ):
        raise UpstreamFailed("telegram", "timeout after 3 attempts")
    failed, started = memory_repo.list_run_events(run.id)
    assert started.status is RunEventStatus.STARTED
    assert (failed.status, failed.duration_ms) == (RunEventStatus.FAILED, 10)
    assert failed.detail == {"error": "telegram failed: timeout after 3 attempts"}


def test_F11_R2_retry_writes_one_retry_per_retry(memory_repo: MemoryRepo, run: Run) -> None:
    calls: list[int] = []

    def flaky() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ConnectTimeout("slow")
        return "sent"

    with stage_step(memory_repo, run.id, Stage.NOTIFY, now=ticking(1)) as step:
        result = retrying("telegram", 3, (1, 4), on_retry=step.retry, sleep=lambda _: None)(flaky)
    assert result == "sent"
    statuses = [e.status for e in reversed(memory_repo.list_run_events(run.id))]
    assert statuses == [
        RunEventStatus.STARTED,
        RunEventStatus.RETRY,
        RunEventStatus.RETRY,
        RunEventStatus.OK,
    ]
    retries = [e for e in reversed(memory_repo.list_run_events(run.id)) if e.detail.get("attempt")]
    assert [e.detail for e in retries] == [
        {"service": "telegram", "attempt": 2, "cause": "timeout"},
        {"service": "telegram", "attempt": 3, "cause": "timeout"},
    ]


def test_retrying_stops_on_4xx() -> None:
    calls: list[int] = []
    retried: list[dict[str, Any]] = []

    def rejected() -> None:
        calls.append(1)
        raise _http_error(400)

    with pytest.raises(UpstreamFailed) as exc:
        retrying("telegram", 3, (1, 4), on_retry=retried.append, sleep=lambda _: None)(rejected)
    assert len(calls) == 1
    assert retried == []
    assert exc.value.service == "telegram"
    assert exc.value.detail == "rejected: HTTP 400"
    assert "example.test" not in str(exc.value)  # URLs may carry a bot token


def test_retrying_raises_upstream_failed_after_last_attempt() -> None:
    calls: list[int] = []
    waits: list[float] = []

    def down() -> None:
        calls.append(1)
        raise _http_error(503)

    with pytest.raises(UpstreamFailed) as exc:
        retrying("supabase", 3, (0.5, 1, 2), sleep=waits.append)(down)
    assert len(calls) == 3
    assert waits == [0.5, 1]
    assert (exc.value.service, exc.value.detail) == ("supabase", "server error after 3 attempts")


def test_retrying_passes_bugs_through_unchanged() -> None:
    def bug() -> None:
        raise KeyError("x")

    with pytest.raises(KeyError):
        retrying("supabase", 3, (0,), sleep=lambda _: None)(bug)


def test_F11_R2_failed_detail_never_quotes_a_url(memory_repo: MemoryRepo, run: Run) -> None:
    with (
        pytest.raises(httpx.HTTPStatusError),
        stage_step(memory_repo, run.id, Stage.NOTIFY, now=ticking(1)),
    ):
        raise _http_error(401)
    failed = memory_repo.list_run_events(run.id)[0]
    assert failed.status is RunEventStatus.FAILED
    assert failed.detail == {"error": "HTTPStatusError: HTTP 401"}


def test_retrying_without_waits_retries_immediately() -> None:
    calls: list[int] = []

    def flaky() -> int:
        calls.append(1)
        if len(calls) < 2:
            raise TimeoutError
        return 7

    assert retrying("supabase", 2, ())(flaky) == 7
