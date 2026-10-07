"""EP-09 and EP-10 over HTTP (004 T043; spec US4, data-model.md §2 to §4, blueprint C25).

The pack is uploaded and validated through the API; its pages are read by the real Phase 2
`extract_page` with a replay model client (committed reads, no network) and reconciled by the
real Phase 1 engine. `cache` seeds the committed reads into the repo, so the pages are
already-read (no model call); the replay client counts any call that is still made.
"""

import hashlib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.core.memory_repo import MemoryRepo
from app.domain.enums import (
    AuditAction,
    Outcome,
    ProofStatus,
    ReviewStatus,
    RunEventStatus,
    RunStatus,
    Stage,
)
from app.domain.models import Extraction
from app.pipeline.model_client import ReplayModelClient
from tests.conftest import store_cached_extractions
from tests.fakes.api import FakeNotifier

API = "/api/v1"
Headers = dict[str, str]

SECTION8_BEFORE_REVIEW = {
    Outcome.MATCHED.value: 21,
    Outcome.MISMATCHED.value: 4,
    Outcome.NEEDS_REVIEW.value: 2,
    Outcome.MISSING.value: 2,
    Outcome.DUPLICATE.value: 1,
    Outcome.NON_RECEIPT.value: 1,
}
"""data-model.md §2 `OutcomeCounts`, pack before review (blueprint Section 8)."""


def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
def replay(client: TestClient, cached_reads_dir: Path) -> Iterator[ReplayModelClient]:
    """The model client EP-09 uses: replays committed reads and counts calls; no backoff."""
    from app.core.deps import get_extract_sleep, get_model_client
    from tests.support.pack_pages import replay_client

    replay = replay_client(cached_reads_dir)
    overrides: dict[Any, Any] = client.app.dependency_overrides  # type: ignore[attr-defined]
    overrides[get_model_client] = lambda: replay
    overrides[get_extract_sleep] = lambda: _no_sleep
    yield replay


@pytest.fixture
def ops_chat(monkeypatch: pytest.MonkeyPatch) -> str:
    from app.core.config import get_settings

    monkeypatch.setenv("TELEGRAM_OPS_CHAT_ID", "ops-chat")
    monkeypatch.setenv("TELEGRAM_CONTROLLER_CHAT_ID", "controller-chat")
    get_settings.cache_clear()
    return "ops-chat"


def seed_page_reads(repo: MemoryRepo, cache_dir: Path, image: bytes) -> int:
    """Store the committed reads (any model / prompt version) of one page image only."""
    sha = hashlib.sha256(image).hexdigest()
    stored = 0
    for path in sorted(cache_dir.glob(f"{sha}.*.json")):
        record = Extraction.model_validate_json(path.read_text(encoding="utf-8"))
        stored += repo.put_extraction(record)[1]
    return stored


@pytest.fixture
def validated(
    client: TestClient,
    tokens: dict[str, Headers],
    pack_upload: Callable[..., dict[str, Any]],
    replay: ReplayModelClient,
) -> Callable[[], str]:
    """`validated()`: a new pack run taken to `VALIDATED` through EP-03 to EP-08."""

    def make() -> str:
        run_id: str = pack_upload()["run"]["id"]
        resp = client.post(f"{API}/runs/{run_id}/validate", headers=tokens["controller"])
        assert resp.status_code == 200, resp.text
        return run_id

    return make


@pytest.fixture
def cached(memory_repo: MemoryRepo, cached_extraction_records: list[Extraction]) -> int:
    """Every committed read in the repo, as the seed script loads them."""
    return store_cached_extractions(memory_repo, cached_extraction_records)


def extract(client: TestClient, headers: Headers, run_id: str, page_no: int, **query: str) -> Any:
    return client.post(
        f"{API}/runs/{run_id}/pages/{page_no}/extract", params=query, headers=headers
    )


def reconcile(client: TestClient, headers: Headers, run_id: str, **query: str) -> Any:
    return client.post(f"{API}/runs/{run_id}/reconcile", params=query, headers=headers)


def extract_all(client: TestClient, headers: Headers, run_id: str) -> None:
    for page in (1, 2):
        resp = extract(client, headers, run_id, page)
        assert resp.status_code == 200, resp.text


def _error(resp: Any) -> dict[str, Any]:
    body: dict[str, Any] = resp.json()["error"]
    return body


def _status(repo: MemoryRepo, run_id: str) -> RunStatus:
    return repo.get_run(UUID(run_id)).status


def _extract_events(repo: MemoryRepo, run_id: str) -> list[Any]:
    return [e for e in repo.list_run_events(UUID(run_id)) if e.stage is Stage.EXTRACT]


def _audit(repo: MemoryRepo, run_id: str, action: AuditAction) -> list[Any]:
    return [a for a in repo.list_audit(run_id=UUID(run_id)) if a.action is action]


def _counts(counts: dict[str, int]) -> dict[str, int]:
    return {o: n for o, n in counts.items() if n}


# --- EP-09 -------------------------------------------------------------------------------


def test_extract_pages_cached_moves_to_extracted(
    client: TestClient,
    tokens: dict[str, Headers],
    memory_repo: MemoryRepo,
    validated: Callable[[], str],
    replay: ReplayModelClient,
    cached: int,
) -> None:
    """US4 scenario 1: cached reads, no model call; EXTRACTING after page 1, EXTRACTED after 2."""
    run_id = validated()
    headers = tokens["controller"]
    first = extract(client, headers, run_id, 1)
    assert first.status_code == 200, first.text
    assert first.json()["page_no"] == 1 and first.json()["cached"] is True
    assert first.json()["rows"] == 14
    assert _status(memory_repo, run_id) is RunStatus.EXTRACTING
    second = extract(client, headers, run_id, 2)
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["page_no"] == 2 and body["cached"] is True and body["rows"] == 14
    assert set(body) == {"page_no", "cached", "rows", "review_cells", "disagreements"}
    assert _status(memory_repo, run_id) is RunStatus.EXTRACTED
    assert replay.calls == 0
    ok = [e for e in _extract_events(memory_repo, run_id) if e.status is RunEventStatus.OK]
    assert sorted((e.detail["page_no"], e.detail["cached"]) for e in ok) == [(1, True), (2, True)]
    audits = _audit(memory_repo, run_id, AuditAction.PAGE_EXTRACTED)
    assert len(audits) == 2
    assert all(a.actor == "controller@kestrel.example" for a in audits)
    detail = client.get(f"{API}/runs/{run_id}", headers=headers).json()
    assert [p["cached"] for p in detail["stage_progress"]["pages"]] == [True, True]


def test_extract_before_validated_409(
    client: TestClient,
    tokens: dict[str, Headers],
    pack_upload: Callable[..., dict[str, Any]],
    replay: ReplayModelClient,
    cached: int,
) -> None:
    run_id = pack_upload()["run"]["id"]
    resp = extract(client, tokens["controller"], run_id, 1)
    assert resp.status_code == 409
    assert _error(resp)["code"] == "RUN_STATE_CONFLICT"
    assert _error(resp)["details"]["status"] == "UPLOADED"
    assert replay.calls == 0


def test_extract_unknown_page_404(
    client: TestClient,
    tokens: dict[str, Headers],
    memory_repo: MemoryRepo,
    validated: Callable[[], str],
    cached: int,
) -> None:
    run_id = validated()
    resp = extract(client, tokens["controller"], run_id, 3)
    assert resp.status_code == 404
    assert _error(resp)["code"] == "NOT_FOUND"
    assert _status(memory_repo, run_id) is RunStatus.VALIDATED
    assert _extract_events(memory_repo, run_id) == []


def test_extract_twice_idempotent(
    client: TestClient,
    tokens: dict[str, Headers],
    memory_repo: MemoryRepo,
    validated: Callable[[], str],
    replay: ReplayModelClient,
    cached: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Section 11 idempotency: a repeat call returns the same result, writes nothing (no
    review-item upsert, no stored read) and calls no model."""
    run_id = validated()
    headers = tokens["controller"]
    first = extract(client, headers, run_id, 1).json()

    def records() -> tuple[int, int, int]:
        rid = UUID(run_id)
        return (
            len(memory_repo.list_run_events(rid)),
            len(memory_repo.list_audit(run_id=rid)),
            len(memory_repo.list_review_items(rid)),
        )

    writes: list[str] = []
    for name in ("upsert_review_items", "put_extraction"):
        real = getattr(memory_repo, name)

        def spy(*args: Any, _name: str = name, _real: Any = real, **kwargs: Any) -> Any:
            writes.append(_name)
            return _real(*args, **kwargs)

        monkeypatch.setattr(memory_repo, name, spy)

    before = records()
    again = extract(client, headers, run_id, 1)  # still EXTRACTING
    assert again.status_code == 200 and again.json() == first
    assert records() == before
    assert writes == []
    assert _status(memory_repo, run_id) is RunStatus.EXTRACTING
    extract(client, headers, run_id, 2)
    writes.clear()
    before = records()
    past = extract(client, headers, run_id, 1)  # EXTRACTED
    assert past.status_code == 200 and past.json() == first
    assert records() == before
    assert writes == []
    assert _status(memory_repo, run_id) is RunStatus.EXTRACTED
    assert replay.calls == 0


def test_extract_retry_after_failure_reads_only_that_page(
    faults_on: None,
    client: TestClient,
    tokens: dict[str, Headers],
    memory_repo: MemoryRepo,
    validated: Callable[[], str],
    replay: ReplayModelClient,
    cached_reads_dir: Path,
    pack_files: dict[str, bytes],
) -> None:
    """FM-04 recovery: page 1 comes from cache; the failed page 2 is read again, alone."""
    seed_page_reads(memory_repo, cached_reads_dir, pack_files["t4_register_page1.jpg"])
    run_id = validated()
    headers = tokens["controller"]
    assert extract(client, headers, run_id, 1).json()["cached"] is True
    failed = extract(client, headers, run_id, 2, fault="EXTRACT_TIMEOUT")
    assert failed.status_code == 502
    run = memory_repo.get_run(UUID(run_id))
    assert run.status is RunStatus.FAILED and run.failed_stage is Stage.EXTRACT
    assert replay.calls == 0

    retried = extract(client, headers, run_id, 2)
    assert retried.status_code == 200, retried.text
    assert retried.json()["cached"] is False
    assert replay.calls == 2  # reads A and B of page 2 only
    run = memory_repo.get_run(UUID(run_id))
    assert run.status is RunStatus.EXTRACTED
    assert run.failed_stage is None and run.error_message is None
    detail = client.get(f"{API}/runs/{run_id}", headers=headers).json()
    assert [(p["page_no"], p["cached"]) for p in detail["stage_progress"]["pages"]] == [
        (1, True),
        (2, False),
    ]


def test_extract_retry_by_ops_only_when_failed(
    faults_on: None,
    client: TestClient,
    tokens: dict[str, Headers],
    memory_repo: MemoryRepo,
    validated: Callable[[], str],
    cached_reads_dir: Path,
    pack_files: dict[str, bytes],
) -> None:
    """contracts/auth.md: OPS may call EP-09 only to retry a FAILED run."""
    run_id = validated()
    assert extract(client, tokens["ops"], run_id, 1).status_code == 403
    assert (
        extract(client, tokens["controller"], run_id, 1, fault="EXTRACT_TIMEOUT").status_code == 502
    )
    seed_page_reads(memory_repo, cached_reads_dir, pack_files["t4_register_page1.jpg"])
    resp = extract(client, tokens["ops"], run_id, 1)
    assert resp.status_code == 200, resp.text
    assert _status(memory_repo, run_id) is RunStatus.EXTRACTING
    assert extract(client, tokens["ops"], run_id, 2).status_code == 403


# --- EP-10 -------------------------------------------------------------------------------


@pytest.fixture
def extracted(
    client: TestClient, tokens: dict[str, Headers], validated: Callable[[], str], cached: int
) -> str:
    run_id = validated()
    extract_all(client, tokens["controller"], run_id)
    return run_id


def test_reconcile_pack_counts_equal_section8(
    client: TestClient, tokens: dict[str, Headers], memory_repo: MemoryRepo, extracted: str
) -> None:
    """US4 scenario 2: real Phase 1 engine over the cached reads; Section 8 "before review"."""
    resp = reconcile(client, tokens["controller"], extracted)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "AWAITING_REVIEW"
    assert body["proof_status"] == "PASS"
    assert set(body["counts"]) == {o.value for o in Outcome}
    assert _counts(body["counts"]) == SECTION8_BEFORE_REVIEW
    assert 0 < body["open_review_items"] <= 10
    assert _status(memory_repo, extracted) is RunStatus.AWAITING_REVIEW
    proof = memory_repo.get_proof(UUID(extracted))
    assert proof is not None and proof.status is ProofStatus.PASS
    detail = client.get(f"{API}/runs/{extracted}", headers=tokens["controller"]).json()
    assert detail["counts"] == body["counts"]
    assert detail["open_review_items"] == body["open_review_items"]


def test_reconcile_twice_identical_result(
    client: TestClient, tokens: dict[str, Headers], memory_repo: MemoryRepo, extracted: str
) -> None:
    first = reconcile(client, tokens["controller"], extracted)
    rid = UUID(extracted)
    groups = memory_repo.list_groups(rid)
    items = memory_repo.list_review_items(rid)
    second = reconcile(client, tokens["controller"], extracted)
    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    assert memory_repo.list_groups(rid) == groups
    assert memory_repo.list_review_items(rid) == items


def test_reconcile_proof_drop_row_fails_at_proof_with_alert(
    faults_on: None,
    ops_chat: str,
    client: TestClient,
    tokens: dict[str, Headers],
    memory_repo: MemoryRepo,
    notifier: FakeNotifier,
    extracted: str,
) -> None:
    """US4 scenario 6 (FM-07 end to end)."""
    resp = reconcile(client, tokens["controller"], extracted, fault="PROOF_DROP_ROW")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "FAILED" and resp.json()["proof_status"] == "FAIL"
    proof = memory_repo.get_proof(UUID(extracted))
    assert proof is not None and proof.status is ProofStatus.FAIL
    run = memory_repo.get_run(UUID(extracted))
    assert run.status is RunStatus.FAILED and run.failed_stage is Stage.PROOF
    assert run.error_message is not None and "failed:" in run.error_message
    assert [chat for chat, _ in notifier.sent] == [ops_chat]
    assert "Stage: proof" in notifier.sent[0][1]


def test_reconcile_proof_drop_row_ignored_when_faults_off(
    client: TestClient, tokens: dict[str, Headers], extracted: str
) -> None:
    resp = reconcile(client, tokens["controller"], extracted, fault="PROOF_DROP_ROW")
    assert resp.status_code == 200 and resp.json()["proof_status"] == "PASS"


def test_reconcile_retry_after_proof_failure(
    faults_on: None,
    client: TestClient,
    tokens: dict[str, Headers],
    memory_repo: MemoryRepo,
    extracted: str,
) -> None:
    reconcile(client, tokens["controller"], extracted, fault="PROOF_DROP_ROW")
    assert reconcile(client, tokens["reviewer"], extracted).status_code == 403
    resp = reconcile(client, tokens["ops"], extracted)  # OPS may retry a FAILED run
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "AWAITING_REVIEW" and body["proof_status"] == "PASS"
    assert _counts(body["counts"]) == SECTION8_BEFORE_REVIEW
    run = memory_repo.get_run(UUID(extracted))
    assert run.status is RunStatus.AWAITING_REVIEW and run.failed_stage is None


def test_stage_on_finalised_run_409(
    client: TestClient,
    tokens: dict[str, Headers],
    memory_repo: MemoryRepo,
    extracted: str,
) -> None:
    """US4 scenario 7: every stage endpoint rejects a FINALISED run."""
    reconcile(client, tokens["controller"], extracted)
    rid = UUID(extracted)
    memory_repo.transition_run(rid, RunStatus.FINALISED, expected={RunStatus.AWAITING_REVIEW})
    headers = tokens["controller"]
    calls = [
        client.post(f"{API}/runs/{extracted}/validate", headers=headers),
        extract(client, headers, extracted, 1),
        reconcile(client, headers, extracted),
    ]
    for resp in calls:
        assert resp.status_code == 409, resp.text
        assert _error(resp)["code"] == "RUN_STATE_CONFLICT"
        assert _error(resp)["details"]["status"] == "FINALISED"


def test_reconcile_accepts_awaiting_review_and_ready(
    client: TestClient, tokens: dict[str, Headers], memory_repo: MemoryRepo, extracted: str
) -> None:
    """Blueprint C25: re-reconcile from AWAITING_REVIEW and from READY gives the same counts."""
    headers = tokens["controller"]
    first = reconcile(client, headers, extracted).json()
    assert first["status"] == "AWAITING_REVIEW"
    again = reconcile(client, headers, extracted)
    assert again.status_code == 200 and again.json()["counts"] == first["counts"]
    memory_repo.transition_run(
        UUID(extracted), RunStatus.READY, expected={RunStatus.AWAITING_REVIEW}
    )
    from_ready = reconcile(client, headers, extracted)
    assert from_ready.status_code == 200, from_ready.text
    assert from_ready.json() == first  # open items remain, so back to AWAITING_REVIEW


def test_reconcile_before_extracted_409(
    client: TestClient,
    tokens: dict[str, Headers],
    memory_repo: MemoryRepo,
    validated: Callable[[], str],
    cached: int,
) -> None:
    headers = tokens["controller"]
    run_id = validated()
    resp = reconcile(client, headers, run_id)
    assert resp.status_code == 409 and _error(resp)["details"]["status"] == "VALIDATED"
    extract(client, headers, run_id, 1)
    resp = reconcile(client, headers, run_id)
    assert resp.status_code == 409 and _error(resp)["details"]["status"] == "EXTRACTING"
    assert memory_repo.get_proof(UUID(run_id)) is None


def test_reconcile_writes_reconciled_audit(
    client: TestClient, tokens: dict[str, Headers], memory_repo: MemoryRepo, extracted: str
) -> None:
    body = reconcile(client, tokens["controller"], extracted).json()
    (entry,) = _audit(memory_repo, extracted, AuditAction.RECONCILED)
    assert entry.run_id == UUID(extracted)
    assert entry.after is not None
    assert entry.after["status"] == "AWAITING_REVIEW"
    assert entry.after["proof_status"] == "PASS"
    assert entry.after["groups"]["MATCHED"] == 21
    assert body["status"] == entry.after["status"]
    assert entry.after["open_review_items"] == body["open_review_items"]  # incl. CELL items


# --- T053: every stage failure path alerts once, ops chat only ----------------------------


def test_reconcile_write_failure_fails_at_reconcile_with_alert(
    ops_chat: str,
    client: TestClient,
    tokens: dict[str, Headers],
    memory_repo: MemoryRepo,
    notifier: FakeNotifier,
    extracted: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FM-08: a write failure mid-reconcile → `FAILED(reconcile)`, one ops alert; retryable."""

    def broken(*_: Any, **__: Any) -> None:
        raise ConnectionError("db went away")

    monkeypatch.setattr(memory_repo, "replace_reconciliation", broken)
    lenient = TestClient(client.app, raise_server_exceptions=False)
    resp = reconcile(lenient, tokens["controller"], extracted)
    assert resp.status_code == 500
    assert _error(resp) == {
        "code": "INTERNAL_ERROR",
        "message": "Unexpected error",
        "stage": "reconcile",
        "details": {},
    }
    run = memory_repo.get_run(UUID(extracted))
    assert run.status is RunStatus.FAILED and run.failed_stage is Stage.RECONCILE
    assert [chat for chat, _ in notifier.attempted] == [ops_chat]
    assert "Stage: reconcile" in notifier.sent[0][1]
    monkeypatch.undo()
    retried = reconcile(client, tokens["ops"], extracted)  # FR-024: retryable
    assert retried.status_code == 200 and retried.json()["status"] == "AWAITING_REVIEW"


def test_extract_missing_page_blob_500_state_unchanged_one_alert(
    ops_chat: str,
    client: TestClient,
    tokens: dict[str, Headers],
    memory_repo: MemoryRepo,
    memory_blobs: Any,
    notifier: FakeNotifier,
    validated: Callable[[], str],
    cached: int,
) -> None:
    """Infrastructure failure (as EP-08): 500, run state unchanged, one ops alert."""
    from app.core.storage import INPUTS_BUCKET

    run_id = validated()
    (page1,) = [f for f in memory_repo.list_source_files(UUID(run_id)) if f.page_no == 1]
    memory_blobs.delete(INPUTS_BUCKET, page1.storage_path)
    resp = extract(client, tokens["controller"], run_id, 1)
    assert resp.status_code == 500 and _error(resp)["stage"] == "extract"
    assert _status(memory_repo, run_id) is RunStatus.VALIDATED
    assert [chat for chat, _ in notifier.attempted] == [ops_chat]


# --- US4 review fixes ----------------------------------------------------------------------


def _corrupt_read(repo: MemoryRepo, cache_dir: Path, image: bytes, variant: str) -> None:
    """Store one committed read of `image` whose result breaks `parse_read` (rows plus blank
    slots no longer 16), so the cache record no longer validates."""
    from tests.support.pack_pages import offline_settings

    s = offline_settings()
    sha = hashlib.sha256(image).hexdigest()
    path = cache_dir / f"{sha}.{variant}.{s.EXTRACT_MODEL}.{s.PROMPT_VERSION}.json"
    record = Extraction.model_validate_json(path.read_text(encoding="utf-8"))
    slots = record.result.blank_row_slots + 1
    broken = record.result.model_copy(update={"blank_row_slots": slots})
    repo.put_extraction(record.model_copy(update={"result": broken}))


def test_extract_corrupt_cached_read_500_failed_one_alert(
    ops_chat: str,
    client: TestClient,
    tokens: dict[str, Headers],
    memory_repo: MemoryRepo,
    notifier: FakeNotifier,
    validated: Callable[[], str],
    cached_reads_dir: Path,
    pack_files: dict[str, bytes],
) -> None:
    """Constitution VIII (fail loudly): an unexpected reader error sets FAILED(extract) with a
    generic message, sends one alert and answers a generic 500; OPS may retry."""
    _corrupt_read(memory_repo, cached_reads_dir, pack_files["t4_register_page1.jpg"], "A")
    run_id = validated()
    resp = extract(client, tokens["controller"], run_id, 1)
    assert resp.status_code == 500
    assert _error(resp) == {
        "code": "INTERNAL_ERROR",
        "message": "Unexpected error",
        "stage": "extract",
        "details": {},
    }
    run = memory_repo.get_run(UUID(run_id))
    assert run.status is RunStatus.FAILED and run.failed_stage is Stage.EXTRACT
    assert run.error_message == "Page 1: internal error (RuntimeError)"
    assert [chat for chat, _ in notifier.attempted] == [ops_chat]
    again = extract(client, tokens["ops"], run_id, 1)  # OPS retry is admitted (fails again)
    assert again.status_code == 500
    assert _status(memory_repo, run_id) is RunStatus.FAILED
    assert len(notifier.attempted) == 1  # same scenario minute: no second alert


def test_extract_repeat_on_past_run_missing_read_409(
    client: TestClient,
    tokens: dict[str, Headers],
    memory_repo: MemoryRepo,
    extracted: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repeat past extract never reads: a read missing under the current model is 409."""
    from app.core.config import get_settings

    monkeypatch.setenv("PROMPT_VERSION", "v-other")
    get_settings.cache_clear()
    resp = extract(client, tokens["controller"], extracted, 1)
    assert resp.status_code == 409 and _error(resp)["code"] == "RUN_STATE_CONFLICT"
    assert _status(memory_repo, extracted) is RunStatus.EXTRACTED


def test_extract_uploaded_run_missing_blob_409_no_alert(
    ops_chat: str,
    client: TestClient,
    tokens: dict[str, Headers],
    memory_repo: MemoryRepo,
    memory_blobs: Any,
    notifier: FakeNotifier,
    pack_upload: Callable[..., dict[str, Any]],
    replay: ReplayModelClient,
) -> None:
    """State, role and accept-set are checked before the page blob is fetched."""
    from app.core.storage import INPUTS_BUCKET

    run_id = pack_upload()["run"]["id"]
    (page1,) = [f for f in memory_repo.list_source_files(UUID(run_id)) if f.page_no == 1]
    memory_blobs.delete(INPUTS_BUCKET, page1.storage_path)
    resp = extract(client, tokens["controller"], run_id, 1)
    assert resp.status_code == 409 and _error(resp)["details"]["status"] == "UPLOADED"
    assert notifier.attempted == []


def test_extract_unreadable_page_422_failed_one_alert(
    ops_chat: str,
    client: TestClient,
    tokens: dict[str, Headers],
    memory_repo: MemoryRepo,
    notifier: FakeNotifier,
    validated: Callable[[], str],
    ground_truth_dir: Path,
) -> None:
    """FM-06 over HTTP: more than half the merged cells ILLEGIBLE → 422, FAILED(extract), one
    alert built from counts only."""
    from app.core.deps import get_model_client
    from app.domain.enums import CellStatus, ReadVariant, RegisterField
    from app.domain.models import Cell
    from tests.fakes.model_clients import ScriptedModelClient
    from tests.support.extracted import ground_truth_read

    gt = ground_truth_read(ground_truth_dir, 1)
    blot = Cell(value=None, status=CellStatus.ILLEGIBLE)
    rows = [r.model_copy(update={"cells": dict.fromkeys(RegisterField, blot)}) for r in gt.rows]
    payload = gt.model_copy(update={"rows": rows}).model_dump(mode="json")
    scripted = ScriptedModelClient({ReadVariant.A: [payload], ReadVariant.B: [payload]})
    overrides: dict[Any, Any] = client.app.dependency_overrides  # type: ignore[attr-defined]
    overrides[get_model_client] = lambda: scripted
    run_id = validated()
    resp = extract(client, tokens["controller"], run_id, 1)
    assert resp.status_code == 422
    error = _error(resp)
    assert (error["code"], error["stage"]) == ("CONTRACT_VIOLATION", "extract")
    assert error["details"] == {"problem": "PAGE_UNREADABLE", "page_no": 1}
    run = memory_repo.get_run(UUID(run_id))
    assert run.status is RunStatus.FAILED and run.failed_stage is Stage.EXTRACT
    assert run.error_message == "Page 1 could not be read, retake the photo"
    assert [chat for chat, _ in notifier.attempted] == [ops_chat]
    assert "cells illegible" in notifier.sent[0][1]


def test_extract_fault_ignored_when_faults_off(
    client: TestClient,
    tokens: dict[str, Headers],
    validated: Callable[[], str],
    cached: int,
) -> None:
    """Section 25: with ALLOW_FAULTS=false, `?fault=` is ignored and the page reads normally."""
    run_id = validated()
    resp = extract(client, tokens["controller"], run_id, 1, fault="EXTRACT_TIMEOUT")
    assert resp.status_code == 200 and resp.json()["cached"] is True


def test_finish_extracting_quiet_when_run_failed_meanwhile(
    memory_repo: MemoryRepo, extracted_run: Any
) -> None:
    """A concurrent page call failed the run after this page succeeded: the move to EXTRACTED
    loses quietly (no register files here, so every page counts as done)."""
    from app.services.stages import _finish_extracting
    from tests.support.pack_pages import offline_settings

    run = memory_repo.transition_run(
        extracted_run.id,
        RunStatus.FAILED,
        expected={RunStatus.EXTRACTED},
        failed_stage=Stage.EXTRACT,
    )
    _finish_extracting(memory_repo, run.id, offline_settings())
    assert memory_repo.get_run(run.id).status is RunStatus.FAILED


def test_reconcile_corrupt_cached_read_500_failed_reconcile_one_alert(
    ops_chat: str,
    client: TestClient,
    tokens: dict[str, Headers],
    memory_repo: MemoryRepo,
    notifier: FakeNotifier,
    extracted: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cached read that no longer validates at reconcile is not "inputs missing": the run is
    FAILED(reconcile), one alert, generic 500."""
    from app.core.errors import InvalidReadOutput
    from app.services import stages

    def corrupt(_: Any) -> Any:
        raise InvalidReadOutput("bad record")

    monkeypatch.setattr(stages, "parse_read", corrupt)
    lenient = TestClient(client.app, raise_server_exceptions=False)
    resp = reconcile(lenient, tokens["controller"], extracted)
    assert resp.status_code == 500
    assert _error(resp)["message"] == "Unexpected error"
    assert _error(resp)["stage"] == "reconcile"
    run = memory_repo.get_run(UUID(extracted))
    assert run.status is RunStatus.FAILED and run.failed_stage is Stage.RECONCILE
    assert run.error_message == "internal error (InvalidReadOutput)"
    assert [chat for chat, _ in notifier.attempted] == [ops_chat]


def test_get_run_open_review_items_follow_engine_rule(
    client: TestClient, tokens: dict[str, Headers], memory_repo: MemoryRepo, extracted: str
) -> None:
    """Decision (a): EP-05 counts open items by the engine's rule (the last reconcile's items
    plus OPEN CELL items), so status and count never contradict; a stored OPEN item the last
    reconcile did not produce does not count."""
    from app.domain.enums import ReviewKind, ReviewReason
    from app.domain.ids import stable_id
    from app.domain.models import ReviewItem

    headers = tokens["controller"]
    rid = UUID(extracted)
    cells = memory_repo.list_review_items(rid, ReviewStatus.OPEN)
    assert cells and all(i.kind is ReviewKind.CELL for i in cells)
    before = client.get(f"{API}/runs/{extracted}", headers=headers).json()
    assert before["open_review_items"] == len(cells)  # before any reconcile: CELL items
    body = reconcile(client, headers, extracted).json()
    (flag_item,) = [
        i for i in memory_repo.list_review_items(rid) if i.reason is ReviewReason.BACKDATED_ENTRY
    ]
    key = "FLAG:BACKDATED_ENTRY:STALE-ROW"
    stale = flag_item.model_copy(update={"id": stable_id(rid, key), "item_key": key})
    assert isinstance(stale, ReviewItem)
    memory_repo.upsert_review_items(rid, [stale])  # an item the last reconcile did not produce
    detail = client.get(f"{API}/runs/{extracted}", headers=headers).json()
    assert detail["open_review_items"] == body["open_review_items"]
    assert detail["status"] == "AWAITING_REVIEW"
