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
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from app.core.config import Settings
from app.core.errors import UpstreamFailed
from app.core.memory_repo import MemoryRepo
from app.domain.enums import ReadVariant, ReviewReason, RunStatus
from app.domain.models import Extraction, PageRead
from app.pipeline import extract
from app.pipeline.extract import extract_page, load_prompt, load_read_schema, merge_reads
from app.pipeline.model_client import RawRead
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


class Sleeps(list[float]):
    """An injected `sleep` that records waits instead of sleeping (thread-safe append)."""

    def __call__(self, seconds: float) -> None:
        self.append(seconds)


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


def _request() -> Any:
    import httpx2

    return httpx2.Request("POST", "https://api.openai.com/v1/responses")


def _timeout() -> Exception:
    import openai

    return openai.APITimeoutError(request=_request())


def _status(cls: Any, code: int) -> Exception:
    import httpx2

    response = httpx2.Response(code, request=_request())
    error: Exception = cls(f"HTTP {code}", response=response, body=None)
    return error
