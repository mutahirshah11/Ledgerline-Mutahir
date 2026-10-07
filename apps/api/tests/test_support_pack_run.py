"""Self-tests of the Phase 4 test helpers `build_reconciled_pack_run` and the `pack_run`,
`fake_notifier` and `api_client_as` fixtures (005 T011, T012)."""

from collections.abc import Callable
from datetime import datetime

from fastapi.testclient import TestClient

from app.core.memory_repo import MemoryRepo
from app.core.storage import INPUTS_BUCKET, MemoryBlobStore
from app.domain.enums import Outcome, ProofStatus, ReviewStatus, RunStatus, Source
from app.services.stages import run_outcome_counts
from tests.fakes.api import FakeNotifier
from tests.support.pack_run import PackRun


def test_pack_run_is_reconciled_before_review(
    pack_run: PackRun,
    memory_repo: MemoryRepo,
    memory_blobs: MemoryBlobStore,
    frozen_clock: datetime,
) -> None:
    assert pack_run.repo is memory_repo and pack_run.blobs is memory_blobs
    run = memory_repo.get_run(pack_run.run_id)
    assert run == pack_run.run and run.status is RunStatus.AWAITING_REVIEW
    assert run.scenario_now == frozen_clock
    counts = run_outcome_counts(memory_repo, run.id)
    assert (counts[Outcome.MATCHED], counts[Outcome.MISMATCHED]) == (21, 4)
    assert (counts[Outcome.MISSING], counts[Outcome.NEEDS_REVIEW]) == (2, 2)
    proof = memory_repo.get_proof(run.id)
    assert proof is not None and proof.status is ProofStatus.PASS
    assert pack_run.result.open_review_items > 0
    assert memory_repo.list_review_items(run.id, ReviewStatus.OPEN)
    for f in memory_repo.list_source_files(run.id):  # page images etc. are in the store
        assert memory_blobs.get(INPUTS_BUCKET, f.storage_path)
    assert len(memory_repo.list_source_files(run.id, Source.REGISTER)) == 2


def test_pack_run_fresh_per_test(pack_run: PackRun, memory_repo: MemoryRepo) -> None:
    """A second test sees exactly one run (nothing shared between tests)."""
    assert [r.id for r in memory_repo.list_runs()] == [pack_run.run_id]


def test_api_client_as_role_reads_the_pack_run(
    api_client_as: Callable[[str], TestClient], pack_run: PackRun, fake_notifier: FakeNotifier
) -> None:
    for role in ("controller", "reviewer", "ops"):
        resp = api_client_as(role).get(f"/api/v1/runs/{pack_run.run_id}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "AWAITING_REVIEW"
    assert api_client_as("stranger").get("/api/v1/runs").status_code == 403
    assert fake_notifier.sent == []


def test_fake_notifier_is_the_app_notifier(
    api_client_as: Callable[[str], TestClient], fake_notifier: FakeNotifier
) -> None:
    from app.core.deps import get_notifier

    client = api_client_as("controller")
    override = client.app.dependency_overrides[get_notifier]  # type: ignore[attr-defined]
    assert override() is fake_notifier


def test_clone_pack_run_copies_are_independent(pack_run_template: PackRun) -> None:
    from app.core import clock
    from app.domain.enums import ReviewStatus
    from tests.support.pack_run import clone_pack_run

    a = clone_pack_run(pack_run_template, MemoryRepo(), MemoryBlobStore())
    b = clone_pack_run(pack_run_template, MemoryRepo(), MemoryBlobStore())
    item = a.repo.list_review_items(a.run_id, ReviewStatus.OPEN)[0]
    a.repo.resolve_review_item(
        item.id,
        status=ReviewStatus.RESOLVED,
        resolved_value="1",
        resolved_by="reviewer@kestrel.example",
        resolved_at=a.run.scenario_now,
        note=None,
    )
    a.blobs.delete(INPUTS_BUCKET, a.repo.list_source_files(a.run_id)[0].storage_path)
    assert b.repo.get_review_item(item.id).status is ReviewStatus.OPEN
    assert len(b.repo.list_source_files(b.run_id)) == 4
    for f in b.repo.list_source_files(b.run_id):
        assert b.blobs.get(INPUTS_BUCKET, f.storage_path)
    assert pack_run_template.repo.get_review_item(item.id).status is ReviewStatus.OPEN
    assert b.run.scenario_now == datetime(2026, 10, 15, 8, 30, tzinfo=clock.KARACHI)


def test_zero_waits_in_phase4_fixtures(pack_run: PackRun) -> None:
    from app.core.config import get_settings

    settings = get_settings()
    assert settings.TELEGRAM_BACKOFF_S == (0,) and settings.EXTRACT_BACKOFF_S == (0,)
