"""F-03 extraction service: cache, parallel reads, retries and failures
(contracts/extract-service.md §3 to §5; FR-001 to FR-014, FR-027 to FR-029). Offline only.

`test_F03_R1_openai_request_shape` (T019) lives in `tests/test_F03_model_client.py` next to the
other `OpenAIModelClient` tests; it is not repeated here.
"""

import base64
import hashlib
import logging
import shutil
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx2
import openai
import pytest

from app.core.config import EXTRACT_ATTEMPTS, EXTRACT_TIMEOUT_S, EXTRACT_WAITS_S, Settings
from app.core.errors import PageUnreadable, UpstreamFailed
from app.core.memory_repo import MemoryRepo
from app.domain.enums import Fault, ReadVariant, ReviewReason, RunStatus
from app.domain.models import Extraction, PageRead
from app.pipeline import extract
from app.pipeline.extract import (
    RetryInfo,
    cache_file_name,
    extract_page,
    load_cached_extractions,
    load_prompt,
    load_read_schema,
    merge_reads,
    parse_read,
)
from app.pipeline.model_client import FaultModelClient, RawRead, ReplayModelClient
from tests.factories import make_audit, make_event, make_run
from tests.fakes.model_clients import Answer, ScriptedModelClient

A, B = ReadVariant.A, ReadVariant.B
RUN_ID = UUID("00000000-0000-4000-8000-000000000031")
OTHER_RUN_ID = UUID("00000000-0000-4000-8000-000000000032")
MONTH = "2026-09"
PAGE_NO = 2
JPEG = b"\xff\xd8\xff\xe0" + b"register-page-two" * 16
SHA = hashlib.sha256(JPEG).hexdigest()
MODEL = "model-under-test"
API_KEY = "-".join(["svc", "test", "credential", "value"])  # built, not a literal secret


def _cell(value: str | None, status: str = "CLEAR", **extra: Any) -> dict[str, Any]:
    return {"value": value, "status": status, "struck": None, "candidates": [], "note": None} | (
        extra
    )


def _row(index: int, **cells: dict[str, Any]) -> dict[str, Any]:
    base = {
        "date": _cell("03/09/26"),
        "grn_no": _cell(f"44{index:02}"),
        "supplier": _cell("Qasr Pkg"),
        "item": _cell("Carton 5ply"),
        "lot_no": _cell(f"L2609-{index:03}"),
        "qty": _cell("120"),
        "unit": _cell("pcs"),
        "recd_by": _cell("AK"),
    }
    return {"row_index": index, "cells": base | cells}


def payload(qty_row2: str = "120", **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "month_written": "Sep 2026",
        "page_no_written": str(PAGE_NO),
        "table_top": 0.1,
        "table_bottom": 0.9,
        "blank_row_slots": 14,
        "rows": [_row(1), _row(2, qty=_cell(qty_row2))],
    }
    return base | over


def raw(p: dict[str, Any], tokens_in: int = 1500, tokens_out: int = 700) -> RawRead:
    return RawRead(payload=p, input_tokens=tokens_in, output_tokens=tokens_out, latency_ms=4321)


def settings(**over: Any) -> Settings:
    values: dict[str, Any] = dict(EXTRACT_MODEL=MODEL, PROMPT_VERSION="v1") | over
    return Settings(_env_file=None, **values)


@pytest.fixture
def repo(frozen_clock: datetime) -> MemoryRepo:
    """A MemoryRepo holding the run (review items need it); the clock is frozen."""
    memory = MemoryRepo()
    memory.create_run(make_run(id=RUN_ID, created_at=frozen_clock, scenario_now=frozen_clock))
    memory.create_run(make_run(id=OTHER_RUN_ID))
    return memory


_RETRYING = threading.local()
"""The variant whose retry the current worker thread last reported (set by `RetryLog`)."""


class Sleeps(list[float]):
    """An injected `sleep` that records waits instead of sleeping (list.append is atomic)."""

    def __init__(self) -> None:
        super().__init__()
        self.variants: list[ReadVariant | None] = []

    def __call__(self, seconds: float) -> None:
        self.variants.append(getattr(_RETRYING, "variant", None))
        self.append(seconds)

    def per_read(self) -> dict[ReadVariant | None, list[float]]:
        """The waits of each read, in order (needs a `RetryLog` as `on_retry`)."""
        grouped: dict[ReadVariant | None, list[float]] = {}
        for variant, seconds in zip(self.variants, self, strict=True):
            grouped.setdefault(variant, []).append(seconds)
        return grouped


class RetryLog(list[RetryInfo]):
    """An `on_retry` observer that records what it hears."""

    def __call__(self, info: RetryInfo) -> None:
        _RETRYING.variant = info.variant  # on_retry runs on the read's thread, just before sleep
        self.append(info)


def run(
    repo: MemoryRepo,
    client: Any,
    *,
    run_id: UUID = RUN_ID,
    image: bytes = JPEG,
    cfg: Settings | None = None,
    sleep: Sleeps | None = None,
    on_retry: Any = None,
) -> Any:
    return extract_page(
        repo=repo,
        client=client,
        run_id=run_id,
        run_month=MONTH,
        page_no=PAGE_NO,
        image=image,
        settings=cfg or settings(),
        on_retry=on_retry,
        sleep=sleep if sleep is not None else Sleeps(),
    )


def healthy(a: Answer | None = None, b: Answer | None = None) -> ScriptedModelClient:
    return ScriptedModelClient(
        {A: [a if a is not None else raw(payload())], B: [b if b is not None else raw(payload())]}
    )


def stored(repo: MemoryRepo, variant: ReadVariant, cfg: Settings | None = None) -> Any:
    cfg = cfg or settings()
    return repo.get_extraction(SHA, variant, cfg.EXTRACT_MODEL, cfg.PROMPT_VERSION)


# --- T019: cache and reads (US2) -----------------------------------------------------------


def test_F03_AC5_cached_rerun_zero_calls(repo: MemoryRepo) -> None:
    first = healthy()
    result1 = run(repo, first)
    assert first.calls == 2
    assert result1.cached is False and result1.calls_made == 2

    second = ScriptedModelClient()  # any call would fail: no scripted answers
    result2 = run(repo, second, run_id=OTHER_RUN_ID)
    assert second.calls == 0
    assert result2.cached is True and result2.calls_made == 0
    assert result2.page == result1.page


def test_F03_R1_one_variant_cached_one_call(repo: MemoryRepo) -> None:
    run(repo, healthy())
    a_record = stored(repo, A)
    fresh = MemoryRepo()  # holds read A only
    fresh.create_run(make_run(id=RUN_ID))
    fresh.put_extraction(a_record)

    client = ScriptedModelClient({B: [raw(payload())]})
    result = run(fresh, client)
    assert client.calls == 1
    assert [r.variant for r in client.requests] == [B]
    assert result.calls_made == 1 and result.cached is False
    assert stored(fresh, A) == a_record  # the cached read is reused, not rewritten
    assert stored(fresh, B) is not None


def _prompts_with_v2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for src in extract.PROMPTS_DIR.iterdir():
        shutil.copy(src, tmp_path / src.name)
        shutil.copy(src, tmp_path / src.name.replace("_v1", "_v2"))
    monkeypatch.setattr(extract, "PROMPTS_DIR", tmp_path)


def test_F03_R1_cache_key_includes_prompt_version(
    repo: MemoryRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prompts_with_v2(tmp_path, monkeypatch)
    run(repo, healthy())
    v1_a, v1_b = stored(repo, A), stored(repo, B)

    client = healthy()
    v2 = settings(PROMPT_VERSION="v2")
    result = run(repo, client, cfg=v2)
    assert client.calls == 2 and result.cached is False
    assert stored(repo, A, v2).prompt_version == "v2"
    assert stored(repo, B, v2).prompt_version == "v2"
    assert (stored(repo, A), stored(repo, B)) == (v1_a, v1_b)  # v1 records untouched


def test_F03_R1_cache_key_includes_model(repo: MemoryRepo) -> None:
    run(repo, healthy())
    old_a, old_b = stored(repo, A), stored(repo, B)

    client = healthy()
    other = settings(EXTRACT_MODEL="other-model")
    result = run(repo, client, cfg=other)
    assert client.calls == 2 and result.cached is False
    assert stored(repo, A, other).model == "other-model"
    assert stored(repo, B, other).model == "other-model"
    assert (stored(repo, A), stored(repo, B)) == (old_a, old_b)  # old-model records untouched


def test_F03_R1_invalid_cached_record_errors(repo: MemoryRepo) -> None:
    run(repo, healthy())
    bad = MemoryRepo()
    bad.create_run(make_run(id=RUN_ID))
    good = stored(repo, A)
    broken = PageRead.model_construct(**(dict(good.result) | {"blank_row_slots": 99}))
    bad.put_extraction(good.model_copy(update={"result": broken}))
    bad.put_extraction(stored(repo, B))

    client = ScriptedModelClient()
    with pytest.raises(RuntimeError, match="cache record invalid"):
        run(bad, client)
    assert client.calls == 0  # an invalid record is an error, never a silent re-read


def test_F03_R2_reads_run_in_parallel(repo: MemoryRepo) -> None:
    barrier = threading.Barrier(2, timeout=5)
    client = ScriptedModelClient({A: [raw(payload())], B: [raw(payload())]}, barrier=barrier)
    result = run(repo, client)  # a sequential caller breaks the barrier and fails
    assert client.calls == 2
    assert {r.variant for r in client.requests} == {A, B}
    assert result.calls_made == 2


def test_F03_R2_request_contains_only_page_image(repo: MemoryRepo) -> None:
    client = healthy()
    run(repo, client)
    by_variant = {r.variant: r for r in client.requests}
    assert set(by_variant) == {A, B}
    for variant, request in by_variant.items():
        assert request.image_sha256 == SHA  # the page image itself, unchanged
        assert request.mime == "image/jpeg"
        # the instructions are exactly the prompt file: no ERP, sheet or other run text
        assert request.instructions == load_prompt(variant, "v1")
        assert request.schema == load_read_schema("v1")
    assert by_variant[A].instructions != by_variant[B].instructions


@pytest.mark.parametrize(
    "image",
    [b"GIF89a" + b"x" * 32, b"%PDF-1.7" + b"x" * 32, b"", b"\xff\xd8"],
    ids=["gif", "pdf", "empty", "short-jpeg"],
)
def test_F03_R2_rejects_non_image_before_call(repo: MemoryRepo, image: bytes) -> None:
    client = healthy()
    with pytest.raises(ValueError, match="not a JPEG or PNG"):
        run(repo, client, image=image)
    assert client.calls == 0


def test_F03_R2_accepts_png(repo: MemoryRepo) -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"png-page" * 8
    client = healthy()
    run(repo, client, image=png)
    assert {r.mime for r in client.requests} == {"image/png"}


def test_F03_R4_invalid_output_never_cached(repo: MemoryRepo) -> None:
    bad = {"rows": "not a list"}
    client = ScriptedModelClient({A: [bad, bad, bad], B: [raw(payload())]})
    with pytest.raises(UpstreamFailed):
        run(repo, client)
    assert stored(repo, A) is None
    assert stored(repo, B) is not None  # the valid sibling read is kept


def test_F03_R10_extraction_record_fields(repo: MemoryRepo, frozen_clock: datetime) -> None:
    run(repo, healthy(a=raw(payload(), 1500, 700), b=raw(payload(), 1600, 800)))
    for variant, (tokens_in, tokens_out) in ((A, (1500, 700)), (B, (1600, 800))):
        record = stored(repo, variant)
        assert record.page_sha256 == SHA
        assert record.read_variant is variant
        assert record.model == MODEL
        assert record.prompt_version == "v1"
        assert (record.input_tokens, record.output_tokens) == (tokens_in, tokens_out)
        assert record.latency_ms == 4321
        assert record.created_at == frozen_clock
        assert record.result == PageRead.model_validate(payload())
    assert stored(repo, A).id != stored(repo, B).id


class RacingRepo(MemoryRepo):
    """Simulates a concurrent writer: lookups always miss, so the put meets a stored record."""

    def get_extraction(self, *args: Any, **kwargs: Any) -> Extraction | None:
        return None


def test_F03_R1_put_conflict_keeps_first(frozen_clock: datetime) -> None:
    racing = RacingRepo()
    racing.create_run(make_run(id=RUN_ID))
    first_a = Extraction(
        id=UUID("00000000-0000-4000-8000-0000000000a1"),
        page_sha256=SHA,
        read_variant=A,
        model=MODEL,
        prompt_version="v1",
        result=PageRead.model_validate(payload(qty_row2="999")),
        input_tokens=1,
        output_tokens=2,
        latency_ms=3,
        created_at=frozen_clock,
    )
    racing.put_extraction(first_a)

    result = run(racing, healthy())  # this call's own read A says qty 120
    kept = MemoryRepo.get_extraction(racing, SHA, A, MODEL, "v1")
    assert kept == first_a  # the first record wins
    # and the page is merged from the stored read, so it stays re-derivable (FR-028)
    qty = result.page.rows[1].cells["qty"]
    assert qty.read_a is not None and qty.read_a.value == "999"


def test_F03_FR027_result_fields(repo: MemoryRepo) -> None:
    client = healthy(a=raw(payload(qty_row2="120")), b=raw(payload(qty_row2="126")))
    result = run(repo, client)
    assert result.page_no == PAGE_NO
    assert result.cached is False
    assert result.calls_made == 2
    assert [r.row_index for r in result.page.rows] == [1, 2]
    assert [i.item_key for i in result.review_items] == ["CELL:REG-p2-r02:qty"]
    assert result.review_items[0].reason is ReviewReason.READS_DISAGREE
    assert result.review_cells == 1
    assert result.disagreements == 1
    assert repo.list_review_items(RUN_ID) == result.review_items


def test_F03_disagreements_count_rows_in_one_read(repo: MemoryRepo) -> None:
    only_a = payload(rows=[_row(1), _row(2), _row(3, item=_cell("x"))], blank_row_slots=13)
    b = payload(rows=[_row(1), _row(2, item=_cell("y"))])
    result = run(repo, healthy(a=raw(only_a), b=raw(b)))
    # row 2 item differs (not a reviewable field, still a disagreement) + row 3 in one read
    assert result.disagreements == 2
    assert [i.item_key for i in result.review_items] == ["CELL:REG-p2-r03:row"]


def test_F03_FR029_no_run_status_events_or_audit(repo: MemoryRepo) -> None:
    repo.add_run_event(make_event(RUN_ID))
    repo.append_audit(make_audit(run_id=RUN_ID))
    before = (repo.get_run(RUN_ID), repo.list_run_events(RUN_ID), repo.list_audit(RUN_ID))
    run(repo, healthy(b=raw(payload(qty_row2="126"))))
    after = (repo.get_run(RUN_ID), repo.list_run_events(RUN_ID), repo.list_audit(RUN_ID))
    assert after == before
    assert after[0].status is RunStatus.CREATED


def test_F03_FR028_rows_rederivable_from_cache(repo: MemoryRepo) -> None:
    result = run(repo, healthy(a=raw(payload(qty_row2="120")), b=raw(payload(qty_row2="126"))))
    rebuilt = merge_reads(
        stored(repo, A).result, stored(repo, B).result, page_no=2, page_sha256=SHA
    )
    assert rebuilt == result.page


def test_F03_logs_contain_no_key_or_image_bytes(
    repo: MemoryRepo, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    run(repo, healthy(), cfg=settings(OPENAI_API_KEY=API_KEY))
    run(repo, ScriptedModelClient(), run_id=OTHER_RUN_ID, cfg=settings(OPENAI_API_KEY=API_KEY))
    text = caplog.text
    assert caplog.records  # the service does log
    assert API_KEY not in text
    assert base64.b64encode(JPEG).decode()[:24] not in text
    assert "register-page-two" not in text
    assert SHA[:12] in text  # pages are identified by hash only


# --- T023: failures and retries (US3) ------------------------------------------------------


def _request() -> httpx2.Request:
    return httpx2.Request("POST", "https://api.openai.com/v1/responses")


def _timeout() -> Exception:
    return openai.APITimeoutError(request=_request())


def _status(cls: type[openai.APIStatusError], code: int) -> Exception:
    return cls(f"HTTP {code}", response=httpx2.Response(code, request=_request()), body=None)


BAD = {"rows": "not a list"}


def _fails(repo: MemoryRepo, client: Any, **kw: Any) -> UpstreamFailed:
    with pytest.raises(UpstreamFailed) as info:
        run(repo, client, **kw)
    return info.value


def _by_variant(client: ScriptedModelClient) -> Counter[ReadVariant]:
    return Counter(r.variant for r in client.requests)


def test_F03_AC7_timeout_three_attempts_upstream_failed(repo: MemoryRepo) -> None:
    client = ScriptedModelClient({A: [_timeout() for _ in range(3)], B: [_timeout()] * 3})
    sleeps, retries = Sleeps(), RetryLog()
    error = _fails(repo, client, sleep=sleeps, on_retry=retries)

    assert _by_variant(client) == {A: 3, B: 3}  # 3 attempts per read
    assert sleeps.per_read() == {A: [2, 8], B: [2, 8]}  # 2 s then 8 s, per read
    assert (error.service, error.stage, error.page_no) == ("openai", "extract", PAGE_NO)
    assert error.detail == "timeout after 3 attempts"
    assert stored(repo, A) is None and stored(repo, B) is None  # cache empty
    assert repo.list_review_items(RUN_ID) == []
    assert sorted((r.variant, r.attempt) for r in retries) == [(A, 2), (A, 3), (B, 2), (B, 3)]


def test_F03_R5_retry_then_success(repo: MemoryRepo) -> None:
    client = ScriptedModelClient({A: [_timeout(), raw(payload())], B: [raw(payload())]})
    sleeps, retries = Sleeps(), RetryLog()
    result = run(repo, client, sleep=sleeps, on_retry=retries)
    assert list(retries) == [RetryInfo(page_no=PAGE_NO, variant=A, attempt=2, cause="timeout")]
    assert list(sleeps) == [2]
    assert stored(repo, A) is not None  # the read that needed a retry is cached
    assert result.calls_made == 3 and result.cached is False


def test_F03_FM05_invalid_output_three_attempts(repo: MemoryRepo) -> None:
    client = ScriptedModelClient({A: [BAD, BAD, BAD], B: [raw(payload())]})
    retries = RetryLog()
    error = _fails(repo, client, on_retry=retries)
    assert error.detail == "invalid output after 3 attempts"
    assert _by_variant(client)[A] == 3
    assert [r.cause for r in retries] == ["invalid output", "invalid output"]


@pytest.mark.parametrize(
    ("error", "cause"),
    [
        (openai.APIConnectionError(request=_request()), "connection error"),
        (_status(openai.RateLimitError, 429), "rate limited"),
        (_status(openai.InternalServerError, 500), "server error"),
        (_status(openai.InternalServerError, 503), "server error"),
    ],
    ids=["connection", "429", "500", "503"],
)
def test_F03_R5_each_retryable_error_class(repo: MemoryRepo, error: Exception, cause: str) -> None:
    client = ScriptedModelClient({A: [error, raw(payload())], B: [raw(payload())]})
    retries = RetryLog()
    result = run(repo, client, on_retry=retries)
    assert _by_variant(client)[A] == 2
    assert list(retries) == [RetryInfo(page_no=PAGE_NO, variant=A, attempt=2, cause=cause)]
    assert result.page.page_no == PAGE_NO


@pytest.mark.parametrize(
    "error_cls, code",
    [(openai.AuthenticationError, 401), (openai.BadRequestError, 400)],
    ids=["401", "400"],
)
def test_F03_R5_auth_error_not_retried(
    repo: MemoryRepo, error_cls: type[openai.APIStatusError], code: int
) -> None:
    client = ScriptedModelClient({A: [_status(error_cls, code)], B: [raw(payload())]})
    sleeps, retries = Sleeps(), RetryLog()
    error = _fails(repo, client, sleep=sleeps, on_retry=retries)
    assert _by_variant(client)[A] == 1  # one attempt, no retry
    assert list(sleeps) == [] and list(retries) == []
    assert error.detail == f"rejected: HTTP {code}"
    assert (error.service, error.stage, error.page_no) == ("openai", "extract", PAGE_NO)


def test_F03_R5_one_read_fails_other_cached(repo: MemoryRepo) -> None:
    failing = ScriptedModelClient({A: [_timeout()] * 3, B: [raw(payload())]})
    _fails(repo, failing)
    assert stored(repo, A) is None
    assert stored(repo, B) is not None  # spec US3 scenario 5
    assert repo.list_review_items(RUN_ID) == []  # no partial page result

    later = ScriptedModelClient({A: [raw(payload())]})
    result = run(repo, later)
    assert later.calls == 1 and [r.variant for r in later.requests] == [A]
    assert result.calls_made == 1


WAIT_S = 5  # a guard against hanging, never a real delay on the passing path


class SignallingRepo(MemoryRepo):
    """Sets `stored[variant]` right after a read is stored, so a blocked read can watch it."""

    def __init__(self) -> None:
        super().__init__()
        self.stored = {v: threading.Event() for v in ReadVariant}

    def put_extraction(self, extraction: Extraction) -> tuple[Extraction, bool]:
        result = super().put_extraction(extraction)
        self.stored[extraction.read_variant].set()
        return result


class HeldClient:
    """Answers B at once; holds A until `release` is set (or `WAIT_S` passes), then answers."""

    def __init__(self, a_answer: Answer, b_answers: list[Answer], release: threading.Event) -> None:
        self.inner = ScriptedModelClient({A: [a_answer], B: b_answers})
        self.release = release
        self.released_in_time: bool | None = None

    def read_page(self, *, variant: ReadVariant, **kwargs: Any) -> RawRead:
        if variant is A:
            self.released_in_time = self.release.wait(timeout=WAIT_S)
        return self.inner.read_page(variant=variant, **kwargs)


@pytest.mark.parametrize(
    "a_answer",
    [raw(payload()), _status(openai.AuthenticationError, 401)],
    ids=["a-succeeds", "a-fails"],
)
def test_F03_FR007_read_stored_before_other_read_finishes(
    frozen_clock: datetime, a_answer: Answer
) -> None:
    signalling = SignallingRepo()
    signalling.create_run(make_run(id=RUN_ID))
    client = HeldClient(a_answer, [raw(payload())], release=signalling.stored[B])
    if isinstance(a_answer, Exception):
        assert _fails(signalling, client).detail == "rejected: HTTP 401"
    else:
        run(signalling, client)
    # read A was still in flight when read B reached the repo (contract §3 step 4)
    assert client.released_in_time is True
    assert stored(signalling, B) is not None


def test_F03_R5_both_fail_reports_read_a_error(repo: MemoryRepo) -> None:
    """B finishes (fails) first; the reported error is still A's (deterministic)."""
    b_done = threading.Event()

    class LastBSignals(ScriptedModelClient):
        def read_page(self, *, variant: ReadVariant, **kwargs: Any) -> RawRead:
            if variant is A:
                assert b_done.wait(timeout=WAIT_S)
            try:
                return super().read_page(variant=variant, **kwargs)
            finally:
                if variant is B and _by_variant(self)[B] == EXTRACT_ATTEMPTS:
                    b_done.set()

    client = LastBSignals({A: [_status(openai.AuthenticationError, 401)], B: [_timeout()] * 3})
    error = _fails(repo, client)
    assert error.detail == "rejected: HTTP 401"


def _response_validation_error() -> Exception:
    return openai.APIResponseValidationError(
        response=httpx2.Response(200, request=_request()), body=None
    )


@pytest.mark.parametrize(
    ("error", "name"),
    [
        (_response_validation_error(), "APIResponseValidationError"),
        (openai.APIError("odd", _request(), body=None), "APIError"),
    ],
    ids=["response-validation", "bare-api-error"],
)
def test_F03_FR012_other_openai_error_is_upstream_failed(
    repo: MemoryRepo, error: Exception, name: str
) -> None:
    client = ScriptedModelClient({A: [error], B: [raw(payload())]})
    sleeps = Sleeps()
    failure = _fails(repo, client, sleep=sleeps)
    assert _by_variant(client)[A] == 1 and list(sleeps) == []  # not retried
    assert (failure.service, failure.stage, failure.page_no) == ("openai", "extract", PAGE_NO)
    assert failure.detail == f"rejected: {name}"
    assert stored(repo, B) is not None


def test_F03_R5_on_retry_error_does_not_abort_read(
    repo: MemoryRepo, caplog: pytest.LogCaptureFixture
) -> None:
    def broken_observer(info: RetryInfo) -> None:
        raise RuntimeError("observer broke")

    client = ScriptedModelClient({A: [_timeout(), raw(payload())], B: [raw(payload())]})
    sleeps = Sleeps()
    result = run(repo, client, sleep=sleeps, on_retry=broken_observer)
    assert _by_variant(client)[A] == 2 and list(sleeps) == [2]  # the retry still happened
    assert result.calls_made == 3 and stored(repo, A) is not None
    assert "on_retry" in caplog.text and "observer broke" in caplog.text


def test_F03_R5_unexpected_error_propagates_unchanged(repo: MemoryRepo) -> None:
    """Errors outside the §4 table (e.g. a replay cache miss) are bugs, not upstream trouble."""
    client = ScriptedModelClient({A: [KeyError("no cached read")], B: [raw(payload())]})
    with pytest.raises(KeyError):
        run(repo, client)
    assert _by_variant(client)[A] == 1
    assert stored(repo, B) is not None


def test_F03_R5_worst_case_budget() -> None:
    worst = EXTRACT_ATTEMPTS * EXTRACT_TIMEOUT_S + sum(EXTRACT_WAITS_S)
    assert (EXTRACT_ATTEMPTS, EXTRACT_TIMEOUT_S, EXTRACT_WAITS_S) == (3, 60, (2, 8))
    assert worst == 190 <= 200  # FR-014, NFR-03 (reads run in parallel: per page too)


def test_F03_FM06_unreadable_page_raises_and_queues_nothing(repo: MemoryRepo) -> None:
    blot = _cell(None, "ILLEGIBLE")
    unreadable = payload(
        rows=[
            _row(1, **{f: blot for f in ("date", "grn_no", "supplier", "item", "lot_no")}),
            _row(2, **{f: blot for f in ("qty", "unit", "recd_by", "item")}),
        ]
    )  # 9 of 16 merged cells illegible
    with pytest.raises(PageUnreadable) as info:
        run(repo, healthy(a=raw(unreadable), b=raw(payload())))
    assert (info.value.page_no, info.value.illegible, info.value.total) == (PAGE_NO, 9, 16)
    assert repo.list_review_items(RUN_ID) == []


def test_F03_FM06_half_illegible_is_readable(repo: MemoryRepo) -> None:
    blot = _cell(None, "ILLEGIBLE")
    half = payload(
        rows=[
            _row(1, **{f: blot for f in ("date", "grn_no", "supplier", "item")}),
            _row(2, **{f: blot for f in ("qty", "unit", "recd_by", "item")}),
        ]
    )
    result = run(repo, healthy(a=raw(half), b=raw(payload())))
    assert result.review_cells > 0


@pytest.mark.parametrize(
    ("fault", "detail"),
    [
        (Fault.EXTRACT_TIMEOUT, "timeout after 3 attempts"),
        (Fault.EXTRACT_BAD_JSON, "invalid output after 3 attempts"),
    ],
)
def test_F03_R5_fault_client_timeout_and_bad_json(
    repo: MemoryRepo, fault: Fault, detail: str
) -> None:
    sleeps = Sleeps()
    error = _fails(repo, FaultModelClient(ScriptedModelClient(), fault), sleep=sleeps)
    assert error.detail == detail
    assert sorted(sleeps) == [2, 2, 8, 8]
    assert stored(repo, A) is None and stored(repo, B) is None


def test_F03_R5_retry_logs_cause_without_secrets(
    repo: MemoryRepo, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    client = ScriptedModelClient({A: [_timeout(), raw(payload())], B: [raw(payload())]})
    run(repo, client, cfg=settings(OPENAI_API_KEY=API_KEY))
    assert "timeout" in caplog.text
    assert API_KEY not in caplog.text
    assert "register-page-two" not in caplog.text


# --- T029: pack cache helpers (contract §5, research R10, FR-031) -----------------------------


def _record(variant: ReadVariant, frozen: datetime, **over: Any) -> Extraction:
    base: dict[str, Any] = dict(
        id=UUID(int=0xE0 + list(ReadVariant).index(variant)),
        page_sha256=SHA,
        read_variant=variant,
        model=MODEL,
        prompt_version="v1",
        result=PageRead.model_validate(payload()),
        input_tokens=1500,
        output_tokens=700,
        latency_ms=4321,
        created_at=frozen,
    )
    return Extraction(**(base | over))


def _write(cache_dir: Path, record: Extraction, name: str | None = None) -> Path:
    path = cache_dir / (
        name
        or cache_file_name(
            record.page_sha256, record.read_variant, record.model, record.prompt_version
        )
    )
    path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
    return path


def test_F03_cache_file_name_format() -> None:
    assert cache_file_name(SHA, A, "gpt-5.6-luna", "v1") == f"{SHA}.A.gpt-5.6-luna.v1.json"
    assert cache_file_name(SHA, B, MODEL, "v2") == f"{SHA}.B.{MODEL}.v2.json"


def test_F03_FR031_load_cached_extractions_idempotent(
    repo: MemoryRepo, tmp_path: Path, frozen_clock: datetime
) -> None:
    records = [_record(A, frozen_clock), _record(B, frozen_clock)]
    for record in records:
        _write(tmp_path, record)
    (tmp_path / ".gitkeep").write_text("")

    assert load_cached_extractions(repo, tmp_path) == 2
    assert load_cached_extractions(repo, tmp_path) == 0  # second load stores nothing new
    for record in records:
        assert stored(repo, record.read_variant) == record

    client = ScriptedModelClient()  # the loaded cache serves the page with no model call
    result = run(repo, client)
    assert client.calls == 0 and result.cached is True


def test_F03_load_cached_extractions_rejects_misnamed_file(
    repo: MemoryRepo, tmp_path: Path, frozen_clock: datetime
) -> None:
    _write(tmp_path, _record(A, frozen_clock), name=f"{SHA}.B.{MODEL}.v1.json")
    with pytest.raises(ValueError, match="does not match its record"):
        load_cached_extractions(repo, tmp_path)
    assert stored(repo, A) is None and stored(repo, B) is None


def test_F03_load_cached_extractions_missing_dir_errors(repo: MemoryRepo, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_cached_extractions(repo, tmp_path / "absent")


@pytest.mark.parametrize("narrow", [False, True], ids=["any-model", "narrowed"])
def test_F03_replay_client_finds_cache_file_name(
    tmp_path: Path, frozen_clock: datetime, narrow: bool
) -> None:
    record = _record(B, frozen_clock)
    _write(tmp_path, record)
    replay = (
        ReplayModelClient(tmp_path, model=MODEL, prompt_version="v1")
        if narrow
        else ReplayModelClient(tmp_path)
    )
    answer = replay.read_page(image=JPEG, mime="image/jpeg", variant=B, instructions="", schema={})
    assert parse_read(answer.payload) == record.result
    assert (answer.input_tokens, answer.output_tokens, answer.latency_ms) == (1500, 700, 4321)
