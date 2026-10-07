"""Engine entry point `reconcile_run` (contracts/engine.md; spec US5, FR-030 to FR-035,
Clarification 4, FM-07, FM-08)."""

import time
from collections import Counter
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

import app.core.memory_repo as memory_repo_module
from app.core.errors import StateConflict
from app.core.memory_repo import MemoryRepo
from app.domain.enums import (
    AuditAction,
    AuditChannel,
    Fault,
    FlagType,
    Outcome,
    ProofStatus,
    RegisterField,
    ReviewKind,
    ReviewReason,
    ReviewStatus,
    RunEventStatus,
    RunStatus,
    Stage,
)
from app.domain.models import Group, Run, RunEvent
from app.pipeline.engine import EngineInputs, proof_failure_message, reconcile_run, run_pipeline
from tests.support.pack import build_engine_inputs, resolved_cell


def _oldest_first(repo: MemoryRepo, run_id: UUID) -> list[RunEvent]:
    """Run events in insertion order: `list_run_events` is newest first, and the frozen clock
    gives every event the same `at` (ties are newest insertion first, C27)."""
    return list(reversed(repo.list_run_events(run_id)))


_RECONCILE_PATHS: dict[RunStatus, tuple[RunStatus, ...]] = {
    RunStatus.RECONCILED: (RunStatus.RECONCILED,),
    RunStatus.FAILED: (RunStatus.FAILED,),
    RunStatus.FINALISED: (RunStatus.RECONCILED, RunStatus.READY, RunStatus.FINALISED),
}


def _state(repo: MemoryRepo, rid: UUID) -> dict[str, str]:
    """Everything a reconcile writes except the append-only logs, as JSON per kind."""
    proof = repo.get_proof(rid)
    return {
        "run": repo.get_run(rid).model_dump_json(),
        "rows": "".join(r.model_dump_json() for r in repo.list_source_rows(rid)),
        "groups": "".join(g.model_dump_json() for g in repo.list_groups(rid)),
        "flags": "".join(f.model_dump_json() for f in repo.list_flags(rid)),
        "norm_log": "".join(n.model_dump_json() for n in repo.list_norm_log(rid)),
        "review": "".join(i.model_dump_json() for i in repo.list_review_items(rid)),
        "proof": "" if proof is None else proof.model_dump_json(),
    }


def _with_decisions(
    inputs: EngineInputs, pack_dir: Path, ground_truth_dir: Path, repo: MemoryRepo
) -> EngineInputs:
    """Inputs for a re-reconcile: the run's stored review items as decisions."""
    stored = repo.list_review_items(inputs.run_id)
    return build_engine_inputs(pack_dir, ground_truth_dir, inputs.run_id, stored)


def _resolve_open_flag_items(repo: MemoryRepo, run_id: UUID, at: datetime) -> None:
    for item in repo.list_review_items(run_id, ReviewStatus.OPEN):
        repo.resolve_review_item(
            item.id,
            status=ReviewStatus.RESOLVED,
            resolved_value=None,
            resolved_by="reviewer@example.com",
            resolved_at=at,
            note="checked with stores",
        )


def test_engine_atomic_write_keeps_previous(
    memory_repo: MemoryRepo,
    extracted_run: Run,
    engine_inputs: EngineInputs,
    pack_dir: Path,
    ground_truth_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = reconcile_run(memory_repo, engine_inputs)
    previous = _state(memory_repo, extracted_run.id)
    audit_before = len(memory_repo.list_audit(extracted_run.id))
    events_before = _oldest_first(memory_repo, extracted_run.id)
    last_key = first.payload.groups[-1].group_key

    real_copy = memory_repo_module._copy

    def failing_copy(model: Any) -> Any:
        if isinstance(model, Group) and model.group_key == last_key:
            raise RuntimeError("write failed on the last group")
        return real_copy(model)

    changed = build_engine_inputs(
        pack_dir,
        ground_truth_dir,
        extracted_run.id,
        [resolved_cell(extracted_run.id, "REG-p2-r05", RegisterField.QTY, "66")],
    )
    with monkeypatch.context() as patch:
        patch.setattr(memory_repo_module, "_copy", failing_copy)
        with pytest.raises(RuntimeError, match="last group"):
            reconcile_run(memory_repo, changed)

    after = _state(memory_repo, extracted_run.id)
    assert {k: v for k, v in after.items() if k != "run"} == {
        k: v for k, v in previous.items() if k != "run"
    }
    run = memory_repo.get_run(extracted_run.id)
    assert (run.status, run.failed_stage) == (RunStatus.FAILED, Stage.RECONCILE)
    assert run.error_message == "internal error (RuntimeError)"
    assert memory_repo.get_group(extracted_run.id, "GRN:4419").outcome is Outcome.NEEDS_REVIEW
    assert len(memory_repo.list_audit(extracted_run.id)) == audit_before
    events = _oldest_first(memory_repo, extracted_run.id)
    assert [(e.stage, e.status) for e in events[len(events_before) :]] == [
        (Stage.RECONCILE, RunEventStatus.STARTED),
        (Stage.PROOF, RunEventStatus.STARTED),
        (Stage.RECONCILE, RunEventStatus.FAILED),
    ]


@pytest.mark.parametrize("method", ["upsert_review_items", "save_proof"])
def test_engine_failure_after_replace_marks_run_failed(
    memory_repo: MemoryRepo,
    extracted_run: Run,
    engine_inputs: EngineInputs,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    """User decision 2026-10-07: any error in steps 4 to 7 (after `replace_reconciliation`
    succeeded) logs `reconcile/FAILED`, moves the run to `FAILED` at `reconcile`, re-raises."""
    error = RuntimeError(f"{method} failed")

    def failing(*_: Any) -> Any:
        raise error

    monkeypatch.setattr(memory_repo, method, failing)  # a repo whose one write fails

    with pytest.raises(RuntimeError) as exc:
        reconcile_run(memory_repo, engine_inputs)

    assert exc.value is error
    assert memory_repo.list_source_rows(extracted_run.id) != []  # the replace went through
    run = memory_repo.get_run(extracted_run.id)
    assert (run.status, run.failed_stage, run.error_message) == (
        RunStatus.FAILED,
        Stage.RECONCILE,
        "internal error (RuntimeError)",
    )
    events = _oldest_first(memory_repo, extracted_run.id)
    assert [(e.stage, e.status) for e in events] == [
        (Stage.RECONCILE, RunEventStatus.STARTED),
        (Stage.PROOF, RunEventStatus.STARTED),
        (Stage.RECONCILE, RunEventStatus.FAILED),
    ]
    assert events[-1].detail == {"error": "internal error (RuntimeError)"}
    assert memory_repo.list_audit(extracted_run.id) == []


def test_engine_pipeline_error_marks_run_failed_at_reconcile(
    memory_repo: MemoryRepo, extracted_run: Run, engine_inputs: EngineInputs
) -> None:
    """User decision 2026-10-07: an error inside `run_pipeline` (here a corrupt store sheet) logs
    `reconcile/FAILED`, moves the run to `FAILED` at `reconcile`, re-raises, writes nothing else."""
    reconcile_run(memory_repo, engine_inputs)
    assert memory_repo.get_run(extracted_run.id).status is RunStatus.AWAITING_REVIEW
    previous = _state(memory_repo, extracted_run.id)
    events_before = _oldest_first(memory_repo, extracted_run.id)
    audit_before = memory_repo.list_audit(extracted_run.id)

    with pytest.raises(ValueError, match="not a readable"):
        reconcile_run(memory_repo, replace(engine_inputs, sheet_bytes=b"not a zip"))

    after = _state(memory_repo, extracted_run.id)
    assert {k: v for k, v in after.items() if k != "run"} == {
        k: v for k, v in previous.items() if k != "run"
    }
    run = memory_repo.get_run(extracted_run.id)
    assert (run.status, run.failed_stage, run.error_message) == (
        RunStatus.FAILED,
        Stage.RECONCILE,
        "internal error (ValueError)",
    )
    events = _oldest_first(memory_repo, extracted_run.id)
    assert events[: len(events_before)] == events_before
    assert [(e.stage, e.status) for e in events[len(events_before) :]] == [
        (Stage.RECONCILE, RunEventStatus.STARTED),
        (Stage.RECONCILE, RunEventStatus.FAILED),
    ]
    assert events[-1].detail == {"error": "internal error (ValueError)"}
    assert memory_repo.list_audit(extracted_run.id) == audit_before


def test_engine_proof_fail_keeps_previous_reconciliation(
    memory_repo: MemoryRepo, extracted_run: Run, engine_inputs: EngineInputs
) -> None:
    reconcile_run(memory_repo, engine_inputs)
    previous = _state(memory_repo, extracted_run.id)
    events_before = _oldest_first(memory_repo, extracted_run.id)
    audit_before = memory_repo.list_audit(extracted_run.id)

    result = reconcile_run(memory_repo, engine_inputs, faults=frozenset({Fault.PROOF_DROP_ROW}))

    assert result.proof.status is ProofStatus.FAIL
    after = _state(memory_repo, extracted_run.id)
    for kind in ("rows", "groups", "flags", "norm_log", "review"):
        assert after[kind] == previous[kind], kind
    stored = memory_repo.get_proof(extracted_run.id)
    assert stored is not None and stored.status is ProofStatus.FAIL
    run = memory_repo.get_run(extracted_run.id)
    assert (run.status, run.failed_stage) == (RunStatus.FAILED, Stage.PROOF)
    assert run.error_message == proof_failure_message(result.proof)
    events = _oldest_first(memory_repo, extracted_run.id)
    assert events[: len(events_before)] == events_before
    assert [(e.stage, e.status) for e in events[len(events_before) :]] == [
        (Stage.RECONCILE, RunEventStatus.STARTED),
        (Stage.PROOF, RunEventStatus.STARTED),
        (Stage.PROOF, RunEventStatus.FAILED),
        (Stage.RECONCILE, RunEventStatus.FAILED),
    ]
    assert memory_repo.list_audit(extracted_run.id) == audit_before


def test_engine_proof_fail_on_finalised_run_keeps_its_proof(
    memory_repo: MemoryRepo,
    extracted_run: Run,
    engine_inputs: EngineInputs,
    pack_dir: Path,
    ground_truth_dir: Path,
    frozen_clock: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run finalised between the status check and the FAIL path: the transition to `FAILED`
    raises `StateConflict` before the finalised run's proof could be overwritten; the failure is
    still logged (`proof/FAILED`, `reconcile/FAILED`)."""
    reconcile_run(memory_repo, engine_inputs)
    _resolve_open_flag_items(memory_repo, extracted_run.id, frozen_clock)
    again = _with_decisions(engine_inputs, pack_dir, ground_truth_dir, memory_repo)
    reconcile_run(memory_repo, again)
    stale = memory_repo.get_run(extracted_run.id)  # READY, as the engine's status check sees it
    memory_repo.update_run_fields(stale.id, run_key="k-final")
    memory_repo.transition_run(
        stale.id,
        RunStatus.FINALISED,
        expected={RunStatus.READY},
        finalised_at=frozen_clock,
        finalised_by="controller@example.com",
    )
    proof = memory_repo.get_proof(stale.id)
    events_before = _oldest_first(memory_repo, stale.id)
    monkeypatch.setattr(memory_repo, "get_run", lambda _: stale)

    with pytest.raises(StateConflict):
        reconcile_run(memory_repo, again, faults=frozenset({Fault.PROOF_DROP_ROW}))

    assert memory_repo.get_proof(stale.id) == proof
    events = _oldest_first(memory_repo, stale.id)
    assert [(e.stage, e.status) for e in events[len(events_before) :]] == [
        (Stage.RECONCILE, RunEventStatus.STARTED),
        (Stage.PROOF, RunEventStatus.STARTED),
        (Stage.PROOF, RunEventStatus.FAILED),
        (Stage.RECONCILE, RunEventStatus.FAILED),
    ]


def test_engine_status_awaiting_review_then_ready(
    memory_repo: MemoryRepo,
    extracted_run: Run,
    engine_inputs: EngineInputs,
    pack_dir: Path,
    ground_truth_dir: Path,
    frozen_clock: datetime,
) -> None:
    reconcile_run(memory_repo, engine_inputs)

    assert memory_repo.get_run(extracted_run.id).status is RunStatus.AWAITING_REVIEW
    open_items = memory_repo.list_review_items(extracted_run.id, ReviewStatus.OPEN)
    assert [(i.kind, i.reason.value) for i in open_items] == [
        (ReviewKind.FLAG_ACK, FlagType.BACKDATED_ENTRY.value),
        (ReviewKind.FLAG_ACK, FlagType.SEQUENCE_GAP.value),
    ]

    _resolve_open_flag_items(memory_repo, extracted_run.id, frozen_clock)
    again = _with_decisions(engine_inputs, pack_dir, ground_truth_dir, memory_repo)
    reconcile_run(memory_repo, again)

    assert memory_repo.get_run(extracted_run.id).status is RunStatus.READY


def test_engine_status_counts_only_current_open_items(
    memory_repo: MemoryRepo,
    extracted_run: Run,
    engine_inputs: EngineInputs,
    pack_dir: Path,
    ground_truth_dir: Path,
    frozen_clock: datetime,
) -> None:
    """User decision 2026-10-07: an OPEN item the current reconcile no longer produces stays
    stored but does not hold the run in `AWAITING_REVIEW` nor count in `open_review_items`."""
    run_id = extracted_run.id
    unknown_supplier = resolved_cell(run_id, "REG-p2-r05", RegisterField.SUPPLIER, "Nobody Ltd")
    first = build_engine_inputs(pack_dir, ground_truth_dir, run_id, [unknown_supplier])
    reconcile_run(memory_repo, first)
    stale_key = "MAP:REG-p2-r05:supplier"
    (stale,) = [i for i in memory_repo.list_review_items(run_id) if i.item_key == stale_key]
    assert stale.status is ReviewStatus.OPEN

    for item in memory_repo.list_review_items(run_id, ReviewStatus.OPEN):
        if item.item_key != stale_key:
            memory_repo.resolve_review_item(
                item.id,
                status=ReviewStatus.RESOLVED,
                resolved_value=None,
                resolved_by="reviewer@example.com",
                resolved_at=frozen_clock,
                note="checked with stores",
            )
    dropped = {stale_key, unknown_supplier.item_key}  # the supplier decision is withdrawn
    decisions = [i for i in memory_repo.list_review_items(run_id) if i.item_key not in dropped]
    again = build_engine_inputs(pack_dir, ground_truth_dir, run_id, decisions)
    result = reconcile_run(memory_repo, again)

    assert stale_key not in {i.item_key for i in result.review_items}
    assert memory_repo.get_run(run_id).status is RunStatus.READY
    assert memory_repo.get_review_item(stale.id) == stale
    entry = max(memory_repo.list_audit(run_id), key=lambda e: e.id)  # this reconcile's entry
    assert entry.after is not None and entry.after["open_review_items"] == 0


def test_engine_open_extraction_cell_items_hold_awaiting_review(
    memory_repo: MemoryRepo,
    extracted_run: Run,
    pack_dir: Path,
    ground_truth_dir: Path,
    frozen_clock: datetime,
) -> None:
    """CELL items come only from extraction (Phase 2), never from the engine: an OPEN one is
    still current, so it holds the run in `AWAITING_REVIEW` and counts in `open_review_items`
    after every flag item is resolved (Phase 1 follow-up; F-08, F-09 guard on READY)."""
    run_id = extracted_run.id
    cell = resolved_cell(
        run_id, "REG-p2-r05", RegisterField.QTY, "66", reason=ReviewReason.CORRECTED
    ).model_copy(
        update={
            "status": ReviewStatus.OPEN,
            "resolved_value": None,
            "resolved_by": None,
            "resolved_at": None,
        }
    )
    memory_repo.upsert_review_items(run_id, [cell])  # queued by extract_page
    reconcile_run(memory_repo, build_engine_inputs(pack_dir, ground_truth_dir, run_id, [cell]))
    for item in memory_repo.list_review_items(run_id, ReviewStatus.OPEN):
        if item.kind is not ReviewKind.CELL:
            memory_repo.resolve_review_item(
                item.id,
                status=ReviewStatus.RESOLVED,
                resolved_value=None,
                resolved_by="reviewer@example.com",
                resolved_at=frozen_clock,
                note="checked with stores",
            )
    stored = memory_repo.list_review_items(run_id)
    reconcile_run(memory_repo, build_engine_inputs(pack_dir, ground_truth_dir, run_id, stored))

    assert memory_repo.get_run(run_id).status is RunStatus.AWAITING_REVIEW
    entry = max(memory_repo.list_audit(run_id), key=lambda e: e.id)
    assert entry.after is not None and entry.after["open_review_items"] == 1


@pytest.mark.parametrize("status", list(_RECONCILE_PATHS))
def test_engine_rejects_other_statuses(
    memory_repo: MemoryRepo, extracted_run: Run, engine_inputs: EngineInputs, status: RunStatus
) -> None:
    run = extracted_run
    for to in _RECONCILE_PATHS[status]:
        failed = {"failed_stage": Stage.EXTRACT} if to is RunStatus.FAILED else {}
        run = memory_repo.transition_run(run.id, to, expected={run.status}, **failed)
    events_before = _oldest_first(memory_repo, run.id)

    with pytest.raises(StateConflict):
        reconcile_run(memory_repo, engine_inputs)

    assert memory_repo.get_run(run.id) == run
    assert memory_repo.list_source_rows(run.id) == []
    assert memory_repo.list_groups(run.id) == []
    assert memory_repo.list_review_items(run.id) == []
    assert memory_repo.get_proof(run.id) is None
    assert _oldest_first(memory_repo, run.id) == events_before
    assert memory_repo.list_audit(run.id) == []


def test_engine_decisions_survive(
    memory_repo: MemoryRepo,
    extracted_run: Run,
    engine_inputs: EngineInputs,
    pack_dir: Path,
    ground_truth_dir: Path,
    frozen_clock: datetime,
) -> None:
    reconcile_run(memory_repo, engine_inputs)
    _resolve_open_flag_items(memory_repo, extracted_run.id, frozen_clock)
    decided = memory_repo.list_review_items(extracted_run.id, ReviewStatus.RESOLVED)
    assert len(decided) == 2

    again = _with_decisions(engine_inputs, pack_dir, ground_truth_dir, memory_repo)
    reconcile_run(memory_repo, again)

    assert memory_repo.list_review_items(extracted_run.id, ReviewStatus.RESOLVED) == decided


def test_engine_events_and_audit(
    memory_repo: MemoryRepo, extracted_run: Run, engine_inputs: EngineInputs, frozen_clock: datetime
) -> None:
    result = reconcile_run(memory_repo, engine_inputs)

    events = _oldest_first(memory_repo, extracted_run.id)
    assert Counter((e.stage, e.status) for e in events) == Counter(
        {
            (Stage.RECONCILE, RunEventStatus.STARTED): 1,
            (Stage.PROOF, RunEventStatus.STARTED): 1,
            (Stage.PROOF, RunEventStatus.OK): 1,
            (Stage.RECONCILE, RunEventStatus.OK): 1,
        }
    )
    assert all(e.at == frozen_clock for e in events)
    assert all(
        e.duration_ms is None or (isinstance(e.duration_ms, int) and e.duration_ms >= 0)
        for e in events
    )
    (reconcile_ok,) = [
        e for e in events if (e.stage, e.status) == (Stage.RECONCILE, RunEventStatus.OK)
    ]
    assert isinstance(reconcile_ok.duration_ms, int) and reconcile_ok.duration_ms >= 0
    counts = result.counts.as_json()
    assert reconcile_ok.detail == counts

    (entry,) = memory_repo.list_audit(extracted_run.id)
    assert (entry.action, entry.actor, entry.channel, entry.entity) == (
        AuditAction.RECONCILED,
        "system",
        AuditChannel.SYSTEM,
        "run",
    )
    assert entry.entity_id == str(extracted_run.id)
    assert entry.at == frozen_clock
    assert entry.after is not None
    open_items = memory_repo.list_review_items(extracted_run.id, ReviewStatus.OPEN)
    assert entry.after == {
        "status": RunStatus.AWAITING_REVIEW.value,
        "proof_status": ProofStatus.PASS.value,
        "open_review_items": len(open_items),
        "open_item_keys": [i.item_key for i in open_items],
        **counts,
    }
    assert result.proof.computed_at == frozen_clock


def test_engine_idempotent(
    memory_repo: MemoryRepo, extracted_run: Run, engine_inputs: EngineInputs
) -> None:
    """Re-reconciling the same inputs leaves run, rows, groups, flags, norm_log, review and proof
    unchanged; run events and audit are append-only and excluded from this comparison (E5,
    constitution VIII)."""
    reconcile_run(memory_repo, engine_inputs)
    first = _state(memory_repo, extracted_run.id)

    reconcile_run(memory_repo, engine_inputs)

    assert _state(memory_repo, extracted_run.id) == first


def test_engine_proof_failure_message_names_checks_and_c3_problems(
    engine_inputs: EngineInputs, frozen_clock: datetime
) -> None:
    """FM-07: `error_message` carries each failing check with its numbers, and C3's problems."""
    proof = run_pipeline(
        engine_inputs, now=frozen_clock, faults=frozenset({Fault.PROOF_DROP_ROW})
    ).proof
    problem = "page 2 read A: 13 rows + 2 blank != 16"
    c3 = proof.checks.C3.model_copy(update={"pass_": False, "problems": [problem]})
    failing = proof.model_copy(update={"checks": proof.checks.model_copy(update={"C3": c3})})

    message = proof_failure_message(failing)

    (dropped,) = proof.checks.C2.unplaced
    assert message.startswith("C1 failed: ")
    assert f"; C2 failed: unplaced [{dropped}]; " in message
    assert (
        "; C3 failed: counted REGISTER 28, ERP 28, SHEET 30, problems [" + problem + "]" in message
    )


def test_engine_proof_failure_message_never_empty_clause(
    engine_inputs: EngineInputs, frozen_clock: datetime
) -> None:
    """A failing check with no listed detail reads `no detail`, never `C2 failed: `."""
    proof = run_pipeline(engine_inputs, now=frozen_clock).proof
    c = proof.checks
    failing = proof.model_copy(
        update={
            "checks": c.model_copy(
                update={
                    "C1": c.C1.model_copy(update={"pass_": False}),
                    "C2": c.C2.model_copy(
                        update={"pass_": False, "unplaced": [], "placed_twice": []}
                    ),
                    "C4": c.C4.model_copy(update={"pass_": False}),
                }
            )
        }
    )

    assert proof_failure_message(failing) == (
        "C1 failed: no detail; C2 failed: no detail; C4 failed: no detail"
    )


def test_engine_reconcile_under_3s(
    memory_repo: MemoryRepo, engine_inputs: EngineInputs, frozen_clock: datetime
) -> None:
    """NFR-05 / SC-005: a full reconcile of the pack completes in under 3 seconds. Only
    `reconcile_run` is timed; fixtures (pack read, run setup) are built before the clock starts."""
    threshold_s = 3.0

    start = time.perf_counter()
    reconcile_run(memory_repo, engine_inputs, now=lambda: frozen_clock)
    elapsed_s = time.perf_counter() - start

    assert elapsed_s < threshold_s
