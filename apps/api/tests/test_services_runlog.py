"""Run log helpers (F-11.R2, research R8): `stage_step` events and the `retrying` policy."""

import traceback
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest

from app.core.errors import InvalidReadOutput, UpstreamFailed
from app.core.memory_repo import MemoryRepo
from app.domain.enums import RunEventStatus, Stage
from app.domain.models import Run, RunEvent
from app.services.runlog import retrying, stage_step, transient_cause
from tests.factories import T0, make_run

NO_SLEEP: Callable[[float], None] = lambda _: None  # noqa: E731


@pytest.fixture
def run(memory_repo: MemoryRepo) -> Run:
    return memory_repo.create_run(make_run())


def ticking(step_ms: int) -> Callable[[], datetime]:
    """A wall clock that moves `step_ms` forward on every call (orders the events)."""
    times: Iterator[datetime] = (T0 + timedelta(milliseconds=step_ms * i) for i in range(1000))
    return lambda: next(times)


def fake_timer(step_s: float) -> Callable[[], float]:
    """A monotonic timer that moves `step_s` seconds forward on every call."""
    ticks: Iterator[float] = (100.0 + step_s * i for i in range(1000))
    return lambda: next(ticks)


def oldest_first(repo: MemoryRepo, run: Run) -> list[RunEvent]:
    return list(reversed(repo.list_run_events(run.id)))


def _http_error(status: int, url: str = "https://api.example.test/send") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", url)
    return httpx.HTTPStatusError("bad", request=request, response=httpx.Response(status))


def test_F11_R2_stage_step_writes_started_ok_with_duration(
    memory_repo: MemoryRepo, run: Run
) -> None:
    with stage_step(
        memory_repo,
        run.id,
        Stage.EXTRACT,
        {"pages": 2},
        page_no=1,
        now=ticking(1),
        timer=fake_timer(0.25),
    ):
        pass
    started, ok = oldest_first(memory_repo, run)
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
        stage_step(
            memory_repo,
            run.id,
            Stage.EXTRACT,
            {"read": "A"},
            page_no=2,
            now=ticking(1),
            timer=fake_timer(0.01),
        ),
    ):
        raise UpstreamFailed("openai", "timeout after 3 attempts")
    started, failed = oldest_first(memory_repo, run)
    assert started.status is RunEventStatus.STARTED
    assert (failed.status, failed.duration_ms) == (RunEventStatus.FAILED, 10)
    assert failed.detail == {
        "read": "A",
        "page_no": 2,
        "error": "openai failed: timeout after 3 attempts",
    }


def test_F11_R2_failed_write_error_never_hides_the_original(run: Run) -> None:
    class BrokenRepo(MemoryRepo):
        def add_run_event(self, event: RunEvent) -> RunEvent:
            if event.status is RunEventStatus.FAILED:
                raise RuntimeError("database down")
            return super().add_run_event(event)

    repo = BrokenRepo()
    repo.create_run(run)
    with pytest.raises(KeyError), stage_step(repo, run.id, Stage.NOTIFY, now=ticking(1)):
        raise KeyError("original")


def test_F11_R2_failed_write_log_never_quotes_the_original(
    run: Run, caplog: pytest.LogCaptureFixture
) -> None:
    class BrokenRepo(MemoryRepo):
        def add_run_event(self, event: RunEvent) -> RunEvent:
            if event.status is RunEventStatus.FAILED:
                raise RuntimeError("database down")
            return super().add_run_event(event)

    repo = BrokenRepo()
    repo.create_run(run)
    secret_url = "https://api.example.test/botSECRET-77/sendMessage"
    with pytest.raises(ValueError), stage_step(repo, run.id, Stage.NOTIFY, now=ticking(1)):
        raise ValueError(secret_url)
    assert caplog.records
    assert "SECRET-77" not in caplog.text
    assert all(r.exc_info is None for r in caplog.records)


def test_F11_R2_base_exception_is_not_logged_as_failed(memory_repo: MemoryRepo, run: Run) -> None:
    with (
        pytest.raises(KeyboardInterrupt),
        stage_step(memory_repo, run.id, Stage.NOTIFY, now=ticking(1)),
    ):
        raise KeyboardInterrupt
    assert [e.status for e in oldest_first(memory_repo, run)] == [RunEventStatus.STARTED]


def test_F11_R2_retry_writes_one_retry_per_retry(memory_repo: MemoryRepo, run: Run) -> None:
    calls: list[int] = []

    def flaky() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ConnectTimeout("slow")
        return "sent"

    with stage_step(memory_repo, run.id, Stage.EXTRACT, page_no=1, now=ticking(1)) as step:
        result = retrying("openai", 3, (1, 4), on_retry=step.retry, sleep=NO_SLEEP)(flaky)
    assert result == "sent"
    events = oldest_first(memory_repo, run)
    assert [e.status for e in events] == [
        RunEventStatus.STARTED,
        RunEventStatus.RETRY,
        RunEventStatus.RETRY,
        RunEventStatus.OK,
    ]
    assert [e.detail for e in events[1:3]] == [
        {"page_no": 1, "service": "openai", "attempt": 2, "cause": "timeout"},
        {"page_no": 1, "service": "openai", "attempt": 3, "cause": "timeout"},
    ]


def test_F11_R2_failed_detail_never_quotes_a_url(memory_repo: MemoryRepo, run: Run) -> None:
    with (
        pytest.raises(httpx.HTTPStatusError),
        stage_step(memory_repo, run.id, Stage.NOTIFY, now=ticking(1)),
    ):
        raise _http_error(401)
    failed = memory_repo.list_run_events(run.id)[0]
    assert failed.status is RunEventStatus.FAILED
    assert failed.detail == {"error": "HTTPStatusError: HTTP 401"}


def test_retrying_stops_on_4xx() -> None:
    calls: list[int] = []
    retried: list[dict[str, Any]] = []

    def rejected() -> None:
        calls.append(1)
        raise _http_error(400)

    with pytest.raises(UpstreamFailed) as exc:
        retrying("telegram", 3, (1, 4), on_retry=retried.append, sleep=NO_SLEEP)(rejected)
    assert len(calls) == 1
    assert retried == []
    assert (exc.value.service, exc.value.detail) == ("telegram", "rejected: HTTP 400")
    assert exc.value.attempts == 1
    assert "example.test" not in str(exc.value)


def test_retrying_retries_429() -> None:
    calls: list[int] = []

    def limited() -> str:
        calls.append(1)
        if len(calls) == 1:
            raise _http_error(429)
        return "ok"

    assert retrying("telegram", 3, (1,), sleep=NO_SLEEP)(limited) == "ok"
    assert len(calls) == 2


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
    assert exc.value.attempts == 3


def test_retrying_passes_upstream_failed_through_unretried() -> None:
    calls: list[int] = []
    original = UpstreamFailed("telegram", "rejected: unexpected answer")

    def rejected() -> None:
        calls.append(1)
        raise original

    with pytest.raises(UpstreamFailed) as exc:
        retrying("telegram", 3, (1,), sleep=NO_SLEEP)(rejected)
    assert exc.value is original
    assert len(calls) == 1
    assert exc.value.attempts == 1  # filled in when the operation left it unset

    preset = UpstreamFailed("openai", "x", attempts=5)

    def raises_preset() -> None:
        raise preset

    with pytest.raises(UpstreamFailed) as kept:
        retrying("openai", 3, (1,), sleep=NO_SLEEP)(raises_preset)
    assert kept.value.attempts == 5


def test_retrying_passes_bugs_through_unchanged() -> None:
    def bug() -> None:
        raise KeyError("x")

    with pytest.raises(KeyError):
        retrying("supabase", 3, (0,), sleep=NO_SLEEP)(bug)


def test_retrying_without_waits_retries_immediately() -> None:
    calls: list[int] = []

    def flaky() -> int:
        calls.append(1)
        if len(calls) < 2:
            raise TimeoutError
        return 7

    assert retrying("supabase", 2, (), sleep=NO_SLEEP)(flaky) == 7


def test_retrying_cause_of_decides_what_retries() -> None:
    calls: list[int] = []
    causes: list[dict[str, Any]] = []

    def odd() -> None:
        calls.append(1)
        raise ValueError("odd")

    with pytest.raises(UpstreamFailed) as exc:
        retrying(
            "x",
            2,
            (0,),
            on_retry=causes.append,
            sleep=NO_SLEEP,
            cause_of=lambda e: "odd" if isinstance(e, ValueError) else None,
        )(odd)
    assert len(calls) == 2
    assert causes == [{"service": "x", "attempt": 2, "cause": "odd"}]
    assert exc.value.detail == "odd after 2 attempts"


def test_retrying_chain_false_keeps_secret_urls_out_of_tracebacks() -> None:
    secret = "".join(["bot", "123", ":", "s" * 20])
    url = f"https://api.example.test/{secret}/send"

    def down() -> None:
        raise _http_error(503, url)

    def rejected() -> None:
        raise _http_error(403, url)

    for fn in (down, rejected):
        with pytest.raises(UpstreamFailed) as exc:
            retrying("telegram", 2, (0,), sleep=NO_SLEEP, chain=False)(fn)
        assert exc.value.__cause__ is None
        assert exc.value.__suppress_context__ is True
        assert secret not in "".join(traceback.format_exception(exc.value))

    with pytest.raises(UpstreamFailed) as chained:
        retrying("supabase", 1, (), sleep=NO_SLEEP)(down)
    assert isinstance(chained.value.__cause__, httpx.HTTPStatusError)


def test_invalid_model_output_is_transient() -> None:
    assert transient_cause(InvalidReadOutput("not json")) == "invalid output"
