"""Engine entry point (contracts/engine.md; spec US5, FR-030 to FR-035).

`run_pipeline` is pure: F-04 → F-05 → F-06 → [fault] → F-07 in memory. `reconcile_run` checks the
run status, runs the pipeline and writes the result, review items, proof, run state, run events
and the `RECONCILED` audit entry. Times come only from the `now` callable (E2).
"""

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from app.core import clock
from app.core.db import Repo
from app.core.errors import StateConflict
from app.domain.enums import (
    AuditAction,
    AuditChannel,
    Fault,
    Outcome,
    ProofStatus,
    ReviewStatus,
    RowKind,
    RunEventStatus,
    RunStatus,
    Source,
    Stage,
)
from app.domain.ids import stable_id
from app.domain.lifecycle import can_transition
from app.domain.models import (
    AuditEntry,
    CanonicalRow,
    ControlFlag,
    Group,
    Proof,
    ReconciliationPayload,
    RegisterPageInput,
    ReviewItem,
    Run,
    RunEvent,
)
from app.pipeline.controls import run_controls
from app.pipeline.normalise import MappingTables, normalise_all, read_erp, read_sheet
from app.pipeline.proof import prove
from app.pipeline.reconcile import reconcile


@dataclass(frozen=True)
class EngineInputs:
    """Everything one reconcile reads (data-model.md Section 2)."""

    run_id: UUID
    month: str  # "YYYY-MM"
    register_pages: list[RegisterPageInput]
    erp_bytes: bytes
    sheet_bytes: bytes
    mappings: MappingTables
    decisions: list[ReviewItem]  # all review items of the run (any status)


@dataclass(frozen=True)
class OutcomeCounts:
    """Groups and rows per outcome, zero-filled for every `Outcome` (Section 8 count tables)."""

    groups_by_outcome: dict[Outcome, int]
    rows_by_outcome: dict[Outcome, dict[Source, int]]

    def as_json(self) -> dict[str, Any]:
        """Plain strings and ints for run event and audit details."""
        return {
            "groups": {o.value: n for o, n in self.groups_by_outcome.items()},
            "rows": {
                o.value: {s.value: n for s, n in by_source.items()}
                for o, by_source in self.rows_by_outcome.items()
            },
        }


@dataclass(frozen=True)
class PipelineResult:
    """Output of `run_pipeline` (data-model.md Section 2)."""

    payload: ReconciliationPayload  # rows, groups, flags (F-04 + F-06), norm_log
    review_items: list[ReviewItem]  # F-04 + F-05 + F-06, sorted by item_key
    proof: Proof
    counts: OutcomeCounts


def _counts(rows: Sequence[CanonicalRow], groups: Sequence[Group]) -> OutcomeCounts:
    by_group = Counter(g.outcome for g in groups)
    by_row = Counter((r.outcome, r.source) for r in rows if r.outcome is not None)
    return OutcomeCounts(
        groups_by_outcome={o: by_group[o] for o in Outcome},
        rows_by_outcome={o: {s: by_row[(o, s)] for s in Source} for o in Outcome},
    )


def _flag_order(flag: ControlFlag) -> tuple[str, bool, int, str]:
    """The F-06 flag order (flag, GRN nulls last, row id), applied to F-04 + F-06 flags."""
    return (flag.flag.value, flag.grn_no is None, flag.grn_no or 0, flag.source_row_id or "")


def _drop_last_placed_row(
    rows: list[CanonicalRow], groups: list[Group]
) -> tuple[list[CanonicalRow], list[Group]]:
    """Fault `PROOF_DROP_ROW` (research R13): un-place the last placed receipt row by sort order;
    its outcome and group become null and it leaves its group's `row_ids`."""
    placed = [
        i for i, r in enumerate(rows) if r.row_kind is RowKind.RECEIPT and r.group_id is not None
    ]
    if not placed:
        return rows, groups
    i = placed[-1]
    row = rows[i]
    rows = [*rows[:i], row.model_copy(update={"outcome": None, "group_id": None}), *rows[i + 1 :]]
    new_groups = []
    for g in groups:
        if g.id == row.group_id:
            kept = [r for r in g.row_ids.get(row.source, []) if r != row.source_row_id]
            row_ids = {s: ids for s, ids in g.row_ids.items() if s is not row.source}
            if kept:
                row_ids[row.source] = kept
            g = g.model_copy(update={"row_ids": row_ids})
        new_groups.append(g)
    return rows, new_groups


def run_pipeline(
    inputs: EngineInputs, *, now: datetime, faults: frozenset[Fault] = frozenset()
) -> PipelineResult:
    """F-04 → F-05 → F-06 → [PROOF_DROP_ROW] → F-07 in memory; writes nothing (FR-030, FR-035).

    Faults other than `PROOF_DROP_ROW` are ignored here (E4)."""
    normalised = normalise_all(
        inputs.run_id,
        inputs.month,
        inputs.register_pages,
        read_erp(inputs.erp_bytes),
        read_sheet(inputs.sheet_bytes, inputs.month),
        inputs.mappings,
        inputs.decisions,
    )
    reconciled = reconcile(inputs.run_id, normalised.rows)
    controls = run_controls(inputs.run_id, inputs.month, reconciled.rows)
    rows, groups = reconciled.rows, reconciled.groups
    if Fault.PROOF_DROP_ROW in faults:
        rows, groups = _drop_last_placed_row(rows, groups)
    flags = sorted([*normalised.flags, *controls.flags], key=_flag_order)
    produced = Counter(r.source for r in normalised.rows)
    rows_in = {s: produced[s] for s in Source}
    proof = prove(inputs.run_id, inputs, rows_in, rows, groups, flags, now)
    review_items = sorted(
        [*normalised.review_items, *reconciled.review_items, *controls.review_items],
        key=lambda item: item.item_key,
    )
    return PipelineResult(
        payload=ReconciliationPayload(
            rows=rows, groups=groups, flags=flags, norm_log=normalised.norm_log
        ),
        review_items=review_items,
        proof=proof,
        counts=_counts(rows, groups),
    )


# --- reconcile_run ------------------------------------------------------------------------

RECONCILABLE: frozenset[RunStatus] = frozenset(
    {RunStatus.EXTRACTED, RunStatus.AWAITING_REVIEW, RunStatus.READY}
)
"""Statuses `reconcile_run` starts from (blueprint C25); any other is a `StateConflict`."""


def _detail(found: list[str]) -> str:
    """The comma-joined detail of one failing check, `no detail` when nothing is listed."""
    return ", ".join(found) or "no detail"


def proof_failure_message(proof: Proof) -> str:
    """The failing checks and their numbers for `Run.error_message` (FM-07), e.g.
    `C1 failed: ERP in 28, placed 27; C2 failed: unplaced [ERP-0027]`."""
    c = proof.checks
    parts: list[str] = []
    if not c.C1.pass_:
        off = [
            f"{s.value} in {n.in_}, placed {n.placed}"
            for s, n in c.C1.per_source.items()
            if n.in_ != n.placed
        ]
        parts.append("C1 failed: " + _detail(off))
    if not c.C2.pass_:
        found = [f"unplaced [{', '.join(c.C2.unplaced)}]"] if c.C2.unplaced else []
        if c.C2.placed_twice:
            found.append(f"placed twice [{', '.join(c.C2.placed_twice)}]")
        parts.append("C2 failed: " + _detail(found))
    if not c.C3.pass_:
        counted = ", ".join(f"{s.value} {n}" for s, n in c.C3.per_source.items())
        listed = f", problems [{', '.join(c.C3.problems)}]" if c.C3.problems else ""
        parts.append(f"C3 failed: counted {counted}{listed}")
    if not c.C4.pass_:
        off = [
            f"{key} in {b.in_}, placed {b.placed}"
            for key, b in c.C4.by_source_unit.items()
            if b.in_ != b.placed
        ]
        parts.append("C4 failed: " + _detail(off))
    if not c.C5.pass_:
        parts.append(f"C5 failed: {c.C5.grns} GRNs, gaps {c.C5.gaps}")
    return "; ".join(parts)


class _Log:
    """Run events for one engine call; ids are deterministic per run and event number."""

    def __init__(self, repo: Repo, run_id: UUID, now: Callable[[], datetime]) -> None:
        self.repo, self.run_id, self.now = repo, run_id, now
        self.started: dict[Stage, datetime] = {}
        self.seq = len(repo.list_run_events(run_id))  # read once; numbered locally after

    def event(self, stage: Stage, status: RunEventStatus, detail: dict[str, Any]) -> None:
        at = self.now()
        if status is RunEventStatus.STARTED:
            self.started[stage] = at
            duration = None
        else:
            duration = (at - self.started[stage]) // timedelta(milliseconds=1)
        seq, self.seq = self.seq, self.seq + 1
        self.repo.add_run_event(
            RunEvent(
                id=stable_id(self.run_id, "run_event", str(seq), stage.value, status.value),
                run_id=self.run_id,
                stage=stage,
                status=status,
                at=at,
                duration_ms=duration,
                detail=detail,
            )
        )


def reconcile_run(
    repo: Repo,
    inputs: EngineInputs,
    *,
    now: Callable[[], datetime] = clock.now,
    faults: frozenset[Fault] = frozenset(),
) -> PipelineResult:
    """Reconcile a run end to end (contracts/engine.md sequence 1 to 8).

    Proof `FAIL`: the run moves to `FAILED` at stage `proof`, then the proof is saved; the previous
    reconciliation is untouched (Clarification 4, FM-07). Any error from step 4 on (replace, review
    items, proof, status moves) logs `reconcile/FAILED`, moves the run to `FAILED` at stage
    `reconcile` when the lifecycle allows, and re-raises; a failed replace keeps the previous
    reconciliation (FM-08; user decision 2026-10-07).
    """
    run_id = inputs.run_id
    run = repo.get_run(run_id)
    if run.status not in RECONCILABLE:
        raise StateConflict(run)

    log = _Log(repo, run_id, now)
    log.event(Stage.RECONCILE, RunEventStatus.STARTED, {})
    result = run_pipeline(inputs, now=now(), faults=faults)
    # contract order: proof/STARTED follows prove() (inside run_pipeline), so the proof stage's
    # duration_ms measures the writes, not the checks
    log.event(Stage.PROOF, RunEventStatus.STARTED, {})

    if result.proof.status is ProofStatus.FAIL:
        _fail_on_proof(repo, log, run, result.proof)
        return result

    try:
        repo.replace_reconciliation(run_id, result.payload)
        repo.upsert_review_items(run_id, result.review_items)
        repo.save_proof(result.proof)
        log.event(Stage.PROOF, RunEventStatus.OK, {"status": result.proof.status.value})
        to, open_count = _advance_status(repo, run)
    except Exception as exc:
        _fail_on_reconcile(repo, log, run_id, exc)
        raise
    _audit_reconciled(repo, log, run, to, open_count, result, now())
    return result


def _fail_on_proof(repo: Repo, log: _Log, run: Run, proof: Proof) -> None:
    """Proof `FAIL` (FM-07): move to `FAILED` first, so a run finalised meanwhile raises
    `StateConflict` before its proof could be overwritten; then save the proof and log."""
    message = proof_failure_message(proof)
    repo.transition_run(
        run.id,
        RunStatus.FAILED,
        expected={run.status},
        failed_stage=Stage.PROOF,
        error_message=message,
    )
    repo.save_proof(proof)
    log.event(Stage.PROOF, RunEventStatus.FAILED, {"error": message})
    log.event(Stage.RECONCILE, RunEventStatus.FAILED, {"error": message})


def _fail_on_reconcile(repo: Repo, log: _Log, run_id: UUID, exc: Exception) -> None:
    """A write in steps 4 to 7 failed: log `reconcile/FAILED` and move the run to `FAILED` at
    stage `reconcile` if its current status allows (a run finalised meanwhile stays put)."""
    message = repr(exc)
    log.event(Stage.RECONCILE, RunEventStatus.FAILED, {"error": message})
    current = repo.get_run(run_id)
    if can_transition(current.status, RunStatus.FAILED):
        repo.transition_run(
            run_id,
            RunStatus.FAILED,
            expected={current.status},
            failed_stage=Stage.RECONCILE,
            error_message=message,
        )


def _advance_status(repo: Repo, run: Run) -> tuple[RunStatus, int]:
    """Step 7: `EXTRACTED` → `RECONCILED`, then `AWAITING_REVIEW` if any review item is open,
    else `READY`. Returns the new status and the open item count."""
    status = run.status
    if status is RunStatus.EXTRACTED:
        status = repo.transition_run(run.id, RunStatus.RECONCILED, expected={status}).status
    open_count = len(repo.list_review_items(run.id, ReviewStatus.OPEN))
    to = RunStatus.AWAITING_REVIEW if open_count else RunStatus.READY
    repo.transition_run(run.id, to, expected={status})
    return to, open_count


def _audit_reconciled(
    repo: Repo,
    log: _Log,
    run: Run,
    to: RunStatus,
    open_count: int,
    result: PipelineResult,
    at: datetime,
) -> None:
    """Step 8: event `reconcile/OK` with counts and the `RECONCILED` audit entry."""
    counts = result.counts.as_json()
    log.event(Stage.RECONCILE, RunEventStatus.OK, counts)
    repo.append_audit(
        AuditEntry(
            id=0,  # assigned by the repo
            at=at,
            actor="system",
            action=AuditAction.RECONCILED,
            entity="run",
            entity_id=str(run.id),
            run_id=run.id,
            before={"status": run.status.value},
            after={
                "status": to.value,
                "proof_status": result.proof.status.value,
                "open_review_items": open_count,
                **counts,
            },
            channel=AuditChannel.SYSTEM,
        )
    )
