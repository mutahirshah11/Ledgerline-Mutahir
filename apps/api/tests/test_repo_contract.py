"""Shared Repo contract suite (contracts/repo-interface.md), parametrised over implementations
by the `repo` fixture in conftest.py (MemoryRepo now; SupabaseRepo in Phase 3)."""

from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.core.db import Repo
from app.core.errors import AppendOnlyViolation, NotFound, StateConflict, UniqueViolation
from app.domain.enums import (
    AuditAction,
    NotificationKind,
    NotificationStatus,
    Outcome,
    ReadVariant,
    ReviewKind,
    ReviewReason,
    ReviewStatus,
    RunStatus,
    Source,
    Stage,
)
from app.domain.lifecycle import ALLOWED_TRANSITIONS
from app.domain.models import CellContext, ReconciliationPayload, ReviewItem, Run
from tests.factories import (
    at,
    cell,
    flag_review,
    make_audit,
    make_event,
    make_extraction,
    make_file,
    make_flag,
    make_group,
    make_norm,
    make_notification,
    make_page,
    make_payload,
    make_proof,
    make_review,
    make_row,
    make_run,
    make_user,
    mapping_review,
    sha,
    unmatched_review,
)

S = RunStatus

REPO_METHODS = {
    "ping",
    "get_allowed_user",
    "upsert_allowed_user",
    "create_run",
    "get_run",
    "list_runs",
    "transition_run",
    "update_run_fields",
    "find_finalised_run_by_key",
    "add_source_file",
    "list_source_files",
    "confirm_source_file",
    "delete_source_file",
    "get_page",
    "add_page_if_new",
    "get_extraction",
    "put_extraction",
    "list_extractions",  # 006 batched read (EP-05)
    "replace_reconciliation",
    "list_source_rows",
    "get_source_row",
    "list_groups",
    "get_group",
    "list_flags",
    "list_norm_log",
    "save_proof",
    "get_proof",
    "upsert_review_items",
    "list_review_items",
    "get_review_item",
    "resolve_review_item",
    "insert_notification",
    "get_notification_by_key",
    "update_notification",
    "list_notifications",
    "list_stale_sending",
    "get_latest_notification",
    "add_run_event",
    "list_run_events",
    "append_audit",
    "list_audit",
}  # data-model.md Section 7: 39 methods, plus 004 E2


def _new_run(repo: Repo, **over: object) -> Run:
    return repo.create_run(make_run(**over))


def _walk(repo: Repo, run: Run, *path: RunStatus) -> Run:
    current = run
    for to in path:
        current = repo.transition_run(current.id, to, expected={current.status})
    return current


def _finalise(repo: Repo, run: Run, run_key: str) -> Run:
    run = repo.update_run_fields(run.id, run_key=run_key)
    run = _walk(
        repo, run, S.UPLOADED, S.VALIDATED, S.EXTRACTING, S.EXTRACTED, S.RECONCILED, S.READY
    )
    return repo.transition_run(
        run.id,
        S.FINALISED,
        expected={S.READY},
        finalised_at=at(60),
        finalised_by="controller@example.com",
    )


# --- protocol shape and health -------------------------------------------------------------


def test_protocol_lists_every_method(repo: Repo) -> None:
    protocol_methods = {n for n in vars(Repo) if not n.startswith("_")}
    assert protocol_methods == REPO_METHODS
    assert isinstance(repo, Repo)


def test_get_repo_memory_backend_is_one_process_wide_repo() -> None:
    """004 T013: REPO_BACKEND=memory (set by conftest) selects one shared MemoryRepo."""
    from app.core.db import get_repo
    from app.core.memory_repo import MemoryRepo

    first = get_repo()
    assert isinstance(first, MemoryRepo)
    assert get_repo() is first


def test_ping(repo: Repo) -> None:
    assert repo.ping() is True


def test_allowed_users_upsert_and_get(repo: Repo) -> None:
    assert repo.get_allowed_user("reviewer@example.com") is None
    repo.upsert_allowed_user(make_user())
    stored = repo.upsert_allowed_user(make_user(display_name="Renamed"))
    assert stored.display_name == "Renamed"
    got = repo.get_allowed_user("reviewer@example.com")
    assert got == stored


# --- general -----------------------------------------------------------------------------


def test_returns_copies(repo: Repo) -> None:
    """G1: mutating a returned object (or the input) never changes stored state."""
    run = _new_run(repo)
    event = make_event(run.id, detail={"n": 1})
    returned = repo.add_run_event(event)
    returned.detail["n"] = 99
    event.detail["n"] = 98
    assert repo.list_run_events(run.id)[0].detail == {"n": 1}

    listed = repo.list_run_events(run.id)
    listed[0].detail["n"] = 97
    listed.clear()
    assert repo.list_run_events(run.id)[0].detail == {"n": 1}

    payload = make_payload(run.id)
    repo.replace_reconciliation(run.id, payload)
    payload.rows[0].raw["qty"] = "mutated"
    rows = repo.list_source_rows(run.id)
    rows[0].raw["qty"] = "mutated"
    assert all(r.raw["qty"] == "1.50" for r in repo.list_source_rows(run.id))

    assert repo.get_run(run.id) is not repo.get_run(run.id)


def test_not_found(repo: Repo) -> None:
    """G4: unknown ids raise NotFound."""
    run = _new_run(repo)
    missing = uuid4()
    calls: list[Callable[[], object]] = [
        lambda: repo.get_run(missing),
        lambda: repo.transition_run(missing, S.UPLOADED, expected={S.CREATED}),
        lambda: repo.update_run_fields(missing, run_key="k"),
        lambda: repo.confirm_source_file(missing, at(1)),
        lambda: repo.delete_source_file(missing),
        lambda: repo.get_source_row(run.id, "ERP-0001"),
        lambda: repo.get_group(run.id, "GRN:1"),
        lambda: repo.get_review_item(missing),
        lambda: repo.resolve_review_item(
            missing,
            status=ReviewStatus.RESOLVED,
            resolved_value="1",
            resolved_by="r@example.com",
            resolved_at=at(1),
            note=None,
        ),
        lambda: repo.update_notification(
            missing, expected={NotificationStatus.PENDING}, updated_at=at(1)
        ),
        lambda: repo.add_source_file(make_file(missing)),
        lambda: repo.replace_reconciliation(missing, make_payload(missing)),
        lambda: repo.add_page_if_new(make_page(missing)),
        lambda: repo.insert_notification(make_notification(missing)),
        lambda: repo.save_proof(make_proof(missing)),
        lambda: repo.add_run_event(make_event(missing)),
        lambda: repo.upsert_review_items(missing, [make_review(missing, "CELL:REG-p1-r01:qty")]),
    ]
    for call in calls:
        with pytest.raises(NotFound):
            call()


# --- runs --------------------------------------------------------------------------------


def test_create_and_get_run(repo: Repo) -> None:
    run = make_run()
    assert repo.create_run(run) == run
    assert repo.get_run(run.id) == run
    with pytest.raises(UniqueViolation) as exc:
        repo.create_run(run)
    assert exc.value.constraint == "runs_pkey"


def test_create_run_cannot_receive_invalid_failed_run(repo: Repo) -> None:
    """FAILED <=> failed_stage is a Run model invariant, so no invalid Run reaches create_run."""
    with pytest.raises(ValidationError, match="failed_stage is set exactly when"):
        make_run(status=S.FAILED, failed_stage=None)
    with pytest.raises(ValidationError, match="failed_stage is set exactly when"):
        make_run(status=S.CREATED, failed_stage=Stage.UPLOAD)
    failed = make_run(status=S.FAILED, failed_stage=Stage.UPLOAD, error_message="boom")
    assert repo.create_run(failed) == failed


def test_transition_enforces_lifecycle(repo: Repo) -> None:
    run = _new_run(repo)
    for to in RunStatus:
        if to in ALLOWED_TRANSITIONS[S.CREATED]:
            continue
        with pytest.raises(StateConflict) as exc:
            repo.transition_run(run.id, to, expected={S.CREATED})
        assert exc.value.current == run
    assert repo.get_run(run.id).status is S.CREATED

    # FAILED requires failed_stage
    with pytest.raises(ValueError):
        repo.transition_run(run.id, S.FAILED, expected={S.CREATED})
    failed = repo.transition_run(
        run.id, S.FAILED, expected={S.CREATED}, failed_stage=Stage.UPLOAD, error_message="boom"
    )
    assert failed.status is S.FAILED
    assert failed.failed_stage is Stage.UPLOAD
    assert failed.error_message == "boom"

    # leaving FAILED must not be handed a failed_stage or error_message
    for bad in ({"failed_stage": Stage.UPLOAD}, {"error_message": "x"}):
        with pytest.raises(ValueError):
            repo.transition_run(run.id, S.CREATED, expected={S.FAILED}, **bad)
    assert repo.get_run(run.id) == failed

    # leaving FAILED clears failed_stage and error_message
    retried = repo.transition_run(run.id, S.CREATED, expected={S.FAILED})
    assert retried.status is S.CREATED
    assert retried.failed_stage is None
    assert retried.error_message is None
    assert repo.get_run(run.id) == retried


def test_transition_field_allow_list(repo: Repo) -> None:
    """Only run_key, error_message, failed_stage, finalised_at, finalised_by may ride along;
    FAILED <=> failed_stage set."""
    run = _new_run(repo)
    bad_fields: list[dict[str, Any]] = [
        {"id": uuid4()},
        {"status": S.READY},
        {"month": "2026-08"},
        {"created_by": "x@example.com"},
        {"created_at": at(9)},
        {"scenario_now": at(9)},
        {"unknown": 1},
    ]
    for bad in bad_fields:
        with pytest.raises(ValueError):
            repo.transition_run(run.id, S.UPLOADED, expected={S.CREATED}, **bad)
    with pytest.raises(ValueError):  # failed_stage only on a transition to FAILED
        repo.transition_run(run.id, S.UPLOADED, expected={S.CREATED}, failed_stage=Stage.UPLOAD)
    assert repo.get_run(run.id) == run
    moved = repo.transition_run(
        run.id, S.UPLOADED, expected={S.CREATED}, run_key="k", error_message="note"
    )
    assert (moved.run_key, moved.error_message) == ("k", "note")


def test_transition_compare_and_set(repo: Repo) -> None:
    run = _new_run(repo)
    moved = repo.transition_run(run.id, S.UPLOADED, expected={S.CREATED})
    with pytest.raises(StateConflict) as exc:
        repo.transition_run(run.id, S.UPLOADED, expected={S.CREATED})
    assert exc.value.current == moved
    assert repo.get_run(run.id) == moved


def test_update_run_fields(repo: Repo) -> None:
    run = _new_run(repo)
    updated = repo.update_run_fields(run.id, run_key="key-1", error_message="note")
    assert updated.run_key == "key-1"
    assert updated.error_message == "note"
    assert updated.status is S.CREATED
    assert repo.get_run(run.id) == updated
    bad_fields: list[dict[str, Any]] = [
        {"status": S.READY},
        {"finalised_by": "x@example.com"},
        {"id": uuid4()},
    ]
    for bad in bad_fields:
        with pytest.raises(ValueError):
            repo.update_run_fields(run.id, **bad)
    with pytest.raises(ValueError):  # failed_stage is set only on a FAILED run
        repo.update_run_fields(run.id, failed_stage=Stage.UPLOAD)

    failed = repo.transition_run(run.id, S.FAILED, expected={S.CREATED}, failed_stage=Stage.UPLOAD)
    with pytest.raises(ValueError):  # a FAILED run always has a failed_stage
        repo.update_run_fields(run.id, failed_stage=None)
    assert repo.get_run(run.id) == failed
    moved = repo.update_run_fields(run.id, failed_stage=Stage.VALIDATE)
    assert moved.failed_stage is Stage.VALIDATE


def test_finalised_is_read_only(repo: Repo) -> None:
    final = _finalise(repo, _new_run(repo), "key-final")
    assert final.status is S.FINALISED
    assert final.finalised_by == "controller@example.com"
    for to in RunStatus:
        with pytest.raises(StateConflict) as exc:
            repo.transition_run(final.id, to, expected={S.FINALISED})
        assert exc.value.current == final
    with pytest.raises(StateConflict):
        repo.update_run_fields(final.id, error_message="x")
    assert repo.get_run(final.id) == final


def test_finalised_run_key_unique(repo: Repo) -> None:
    _finalise(repo, _new_run(repo), "same-key")
    loser = _new_run(repo)
    with pytest.raises(UniqueViolation) as exc:
        _finalise(repo, loser, "same-key")
    assert exc.value.constraint == "runs_run_key_finalised_uq"
    assert repo.get_run(loser.id).status is S.READY


def test_list_runs_newest_first(repo: Repo) -> None:
    a = _new_run(repo, created_at=at(1))
    c = _new_run(repo, created_at=at(3))
    tie = sorted([make_run(created_at=at(2)), make_run(created_at=at(2))], key=lambda r: r.id)
    for r in reversed(tie):
        repo.create_run(r)
    assert [r.id for r in repo.list_runs()] == [c.id, tie[0].id, tie[1].id, a.id]
    assert [r.id for r in repo.list_runs(limit=2)] == [c.id, tie[0].id]


def test_find_finalised_by_key_ignores_unfinalised(repo: Repo) -> None:
    pending = _new_run(repo)
    repo.update_run_fields(pending.id, run_key="k1")
    assert repo.find_finalised_run_by_key("k1") is None
    final = _finalise(repo, _new_run(repo), "k1")
    assert repo.find_finalised_run_by_key("k1") == final
    assert repo.find_finalised_run_by_key("other") is None


# --- source files and pages --------------------------------------------------------------


def test_source_file_unique_run_sha(repo: Repo) -> None:
    run = _new_run(repo)
    other = _new_run(repo)
    first = repo.add_source_file(make_file(run.id, sha256=sha(1)))
    with pytest.raises(UniqueViolation) as exc:
        repo.add_source_file(make_file(run.id, sha256=sha(1)))
    assert exc.value.constraint == "source_files_run_sha256_uq"
    with pytest.raises(UniqueViolation) as exc:
        repo.add_source_file(make_file(run.id, id=first.id, sha256=sha(2)))
    assert exc.value.constraint == "source_files_pkey"
    repo.add_source_file(make_file(other.id, sha256=sha(1)))  # other run: allowed
    assert len(repo.list_source_files(run.id)) == 1


def test_source_file_add_only_while_created(repo: Repo) -> None:
    """EP-06 racing EP-07: no file is added once the run left CREATED (0002 insert trigger)."""
    run = _new_run(repo)
    repo.add_source_file(make_file(run.id, sha256=sha(1)))
    repo.transition_run(run.id, S.UPLOADED, expected={S.CREATED})
    with pytest.raises(StateConflict) as exc:
        repo.add_source_file(make_file(run.id, sha256=sha(2)))
    assert exc.value.current.status is S.UPLOADED
    assert len(repo.list_source_files(run.id)) == 1


def test_source_files_list_confirm_delete(repo: Repo) -> None:
    run = _new_run(repo)
    erp = repo.add_source_file(make_file(run.id, sha256=sha(1)))
    reg = repo.add_source_file(
        make_file(run.id, source=Source.REGISTER, sha256=sha(2), page_no=1, filename="p1.jpg")
    )
    assert [f.id for f in repo.list_source_files(run.id)] == [erp.id, reg.id]
    assert [f.id for f in repo.list_source_files(run.id, Source.REGISTER)] == [reg.id]

    confirmed = repo.confirm_source_file(erp.id, at(5))
    assert confirmed.confirmed_at == at(5)
    assert repo.list_source_files(run.id, Source.ERP) == [confirmed]

    repo.delete_source_file(reg.id)
    assert [f.id for f in repo.list_source_files(run.id)] == [erp.id]
    # the (run, sha) slot is free again
    repo.add_source_file(make_file(run.id, source=Source.REGISTER, sha256=sha(2), page_no=1))

    repo.transition_run(run.id, S.UPLOADED, expected={S.CREATED})
    with pytest.raises(StateConflict):
        repo.delete_source_file(erp.id)
    assert len(repo.list_source_files(run.id)) == 2


def test_add_page_if_new_idempotent(repo: Repo) -> None:
    run = _new_run(repo)
    other = _new_run(repo)
    assert repo.get_page(sha(7)) is None
    first, created = repo.add_page_if_new(make_page(run.id))
    assert created is True
    again, created2 = repo.add_page_if_new(make_page(other.id, page_no=2, first_seen_at=at(9)))
    assert created2 is False
    assert again == first
    assert repo.get_page(sha(7)) == first


# --- extraction cache --------------------------------------------------------------------


def test_put_extraction_never_overwrites(repo: Repo) -> None:
    assert repo.get_extraction(sha(7), ReadVariant.A, "model-x", "v1") is None
    first, created = repo.put_extraction(make_extraction(input_tokens=1))
    assert created is True
    second, created2 = repo.put_extraction(make_extraction(input_tokens=2))
    assert created2 is False
    assert second == first
    assert repo.get_extraction(sha(7), ReadVariant.A, "model-x", "v1") == first
    # a different key component is a different entry
    _, created3 = repo.put_extraction(make_extraction(read_variant=ReadVariant.B))
    assert created3 is True
    assert repo.get_extraction(sha(7), ReadVariant.B, "model-x", "v1") is not None
    assert repo.get_extraction(sha(7), ReadVariant.A, "model-x", "v2") is None
    # the same id under a new key collides on the primary key
    with pytest.raises(UniqueViolation) as exc:
        repo.put_extraction(make_extraction(id=first.id, prompt_version="v9"))
    assert exc.value.constraint == "extractions_pkey"
    assert repo.get_extraction(sha(7), ReadVariant.A, "model-x", "v9") is None


# --- reconciliation ----------------------------------------------------------------------


def _assert_state(repo: Repo, rid: UUID, payload: ReconciliationPayload) -> None:
    assert sorted(repo.list_source_rows(rid), key=lambda r: r.source_row_id) == sorted(
        payload.rows, key=lambda r: r.source_row_id
    )
    assert sorted(repo.list_groups(rid), key=lambda g: g.group_key) == sorted(
        payload.groups, key=lambda g: g.group_key
    )
    assert repo.list_flags(rid) == payload.flags
    assert repo.list_norm_log(rid) == payload.norm_log


def test_replace_reconciliation_replaces_exactly(repo: Repo) -> None:
    run = _new_run(repo)
    other = _new_run(repo)
    other_payload = make_payload(other.id, 3)
    repo.replace_reconciliation(other.id, other_payload)

    first = make_payload(run.id, 1)
    repo.replace_reconciliation(run.id, first)
    _assert_state(repo, run.id, first)

    second = make_payload(run.id, 2)
    # keep one row id across reconciles (upsert on run_id, source_row_id)
    second = second.model_copy(
        update={"rows": [*second.rows, first.rows[1].model_copy(update={"item": "changed"})]}
    )
    second = ReconciliationPayload.model_validate(second.model_dump())
    repo.replace_reconciliation(run.id, second)
    _assert_state(repo, run.id, second)
    assert repo.get_source_row(run.id, first.rows[1].source_row_id).item == "changed"
    with pytest.raises(NotFound):
        repo.get_source_row(run.id, first.rows[0].source_row_id)
    with pytest.raises(NotFound):
        repo.get_group(run.id, first.groups[0].group_key)
    assert repo.get_group(run.id, second.groups[0].group_key) == second.groups[0]

    _assert_state(repo, other.id, other_payload)  # other run untouched


def test_replace_reconciliation_atomic_on_failure(repo: Repo) -> None:
    run = _new_run(repo)
    before = make_payload(run.id, 1)
    repo.replace_reconciliation(run.id, before)

    good = make_group(run.id, "GRN:7", grn_no=7)
    clash = make_group(run.id, "GRN:7", grn_no=7)  # last group violates (run_id, group_key)
    bad = ReconciliationPayload(
        rows=[make_row(run.id, "ERP-0007", group_id=good.id)],
        groups=[good, clash],
        flags=[make_flag(run.id)],
        norm_log=[make_norm(run.id, "ERP-0007")],
    )
    with pytest.raises(UniqueViolation) as exc:
        repo.replace_reconciliation(run.id, bad)
    assert exc.value.constraint == "groups_run_group_key_uq"
    _assert_state(repo, run.id, before)

    dup_rows = ReconciliationPayload(
        rows=[make_row(run.id, "ERP-0007"), make_row(run.id, "ERP-0007")],
        groups=[],
        flags=[],
        norm_log=[],
    )
    with pytest.raises(UniqueViolation) as exc:
        repo.replace_reconciliation(run.id, dup_rows)
    assert exc.value.constraint == "source_rows_run_row_uq"
    _assert_state(repo, run.id, before)

    row = make_row(run.id, "ERP-0007")
    dup_row_ids = ReconciliationPayload(
        rows=[row, make_row(run.id, "ERP-0008", id=row.id)],
        groups=[],
        flags=[],
        norm_log=[],
    )
    with pytest.raises(UniqueViolation) as exc:
        repo.replace_reconciliation(run.id, dup_row_ids)
    assert exc.value.constraint == "source_rows_pkey"

    dup_group_ids = ReconciliationPayload(
        rows=[],
        groups=[good, make_group(run.id, "GRN:8", grn_no=8, id=good.id)],
        flags=[],
        norm_log=[],
    )
    with pytest.raises(UniqueViolation) as exc:
        repo.replace_reconciliation(run.id, dup_group_ids)
    assert exc.value.constraint == "groups_pkey"
    _assert_state(repo, run.id, before)


def test_replace_reconciliation_rejects_finalised_run(repo: Repo) -> None:
    run = _new_run(repo)
    before = make_payload(run.id, 1)
    repo.replace_reconciliation(run.id, before)
    final = _finalise(repo, repo.get_run(run.id), "k-final")
    with pytest.raises(StateConflict) as exc:
        repo.replace_reconciliation(run.id, make_payload(run.id, 2))
    assert exc.value.current == final
    _assert_state(repo, run.id, before)


def test_replace_reconciliation_rejects_duplicate_ids(repo: Repo) -> None:
    """Flag and norm-log ids are primary keys: no repeats in the payload, nor ids held by
    another run's reconcile outputs. Nothing is written on failure."""
    run = _new_run(repo)
    other = _new_run(repo)
    theirs = make_payload(other.id, 3)
    repo.replace_reconciliation(other.id, theirs)
    before = make_payload(run.id, 1)
    repo.replace_reconciliation(run.id, before)

    flag = make_flag(run.id)
    norm = make_norm(run.id, "ERP-0007")
    empty = ReconciliationPayload(rows=[], groups=[], flags=[], norm_log=[])
    cases: list[tuple[ReconciliationPayload, str]] = [
        (
            empty.model_copy(update={"flags": [flag, make_flag(run.id, id=flag.id)]}),
            "control_flags_pkey",
        ),
        (
            empty.model_copy(
                update={"norm_log": [norm, make_norm(run.id, "ERP-0008", id=norm.id)]}
            ),
            "normalisation_log_pkey",
        ),
        # ids already used by another run
        (
            empty.model_copy(update={"flags": [make_flag(run.id, id=theirs.flags[0].id)]}),
            "control_flags_pkey",
        ),
        (
            empty.model_copy(
                update={"norm_log": [make_norm(run.id, "ERP-0007", id=theirs.norm_log[0].id)]}
            ),
            "normalisation_log_pkey",
        ),
        (
            empty.model_copy(update={"rows": [make_row(run.id, "ERP-0007", id=theirs.rows[0].id)]}),
            "source_rows_pkey",
        ),
        (
            empty.model_copy(
                update={"groups": [make_group(run.id, "GRN:7", grn_no=7, id=theirs.groups[0].id)]}
            ),
            "groups_pkey",
        ),
    ]
    for payload, constraint in cases:
        with pytest.raises(UniqueViolation) as exc:
            repo.replace_reconciliation(run.id, payload)
        assert exc.value.constraint == constraint
        _assert_state(repo, run.id, before)
    _assert_state(repo, other.id, theirs)

    # the run's own previous ids are free again: the old outputs are replaced
    repo.replace_reconciliation(run.id, before)
    _assert_state(repo, run.id, before)


def test_replace_reconciliation_duplicate_precedence(repo: Repo) -> None:
    """Like the SQL, the first broken key in this order is reported: source_row_id, row id,
    group_key, group id, flag id, norm-log id."""
    run = _new_run(repo)
    row = make_row(run.id, "ERP-0007")
    flag = make_flag(run.id)
    payload = ReconciliationPayload(
        rows=[row, make_row(run.id, "ERP-0008", id=row.id)],
        groups=[],
        flags=[flag, make_flag(run.id, id=flag.id)],
        norm_log=[],
    )
    with pytest.raises(UniqueViolation) as exc:
        repo.replace_reconciliation(run.id, payload)
    assert exc.value.constraint == "source_rows_pkey"


def test_replace_never_touches_review_items(repo: Repo) -> None:
    run = _new_run(repo)
    repo.upsert_review_items(run.id, [make_review(run.id, "CELL:REG-p1-r01:qty")])
    repo.save_proof(make_proof(run.id))
    repo.insert_notification(make_notification(run.id))
    repo.add_run_event(make_event(run.id))
    repo.append_audit(make_audit(run_id=run.id))
    snapshot = (
        repo.list_review_items(run.id),
        repo.get_proof(run.id),
        repo.list_notifications(run.id),
        repo.list_run_events(run.id),
        repo.list_audit(run.id),
    )
    repo.replace_reconciliation(run.id, make_payload(run.id, 1))
    repo.replace_reconciliation(run.id, make_payload(run.id, 2))
    assert (
        repo.list_review_items(run.id),
        repo.get_proof(run.id),
        repo.list_notifications(run.id),
        repo.list_run_events(run.id),
        repo.list_audit(run.id),
    ) == snapshot


def test_rows_and_groups_sorted(repo: Repo) -> None:
    run = _new_run(repo)
    rid = run.id
    groups = [
        make_group(rid, "LOT:b|S1"),
        make_group(rid, "GRN:10", grn_no=10),
        make_group(rid, "LOT:a|S1", outcome=Outcome.MISSING, missing_from=[Source.ERP]),
        make_group(rid, "GRN:9", grn_no=9),
    ]
    rows = [
        make_row(rid, i)
        for i in ("XLS-r3", "ERP-0002", "REG-p1-r02", "XLS-r2", "ERP-0001", "REG-p1-r01")
    ]
    repo.replace_reconciliation(
        rid, ReconciliationPayload(rows=rows, groups=groups, flags=[], norm_log=[])
    )
    assert [g.group_key for g in repo.list_groups(rid)] == [
        "GRN:9",
        "GRN:10",
        "LOT:a|S1",
        "LOT:b|S1",
    ]
    assert [g.group_key for g in repo.list_groups(rid, Outcome.MISSING)] == ["LOT:a|S1"]
    assert [r.source_row_id for r in repo.list_source_rows(rid)] == [
        "REG-p1-r01",
        "REG-p1-r02",
        "ERP-0001",
        "ERP-0002",
        "XLS-r2",
        "XLS-r3",
    ]
    assert [r.source_row_id for r in repo.list_source_rows(rid, Source.SHEET)] == [
        "XLS-r2",
        "XLS-r3",
    ]


def test_flags_and_norm_log_filters(repo: Repo) -> None:
    run = _new_run(repo)
    rid = run.id
    flags = [make_flag(rid, detail={"i": "2"}), make_flag(rid, detail={"i": "1"})]
    norms = [make_norm(rid, "ERP-0002"), make_norm(rid, "ERP-0001"), make_norm(rid, "ERP-0002")]
    repo.replace_reconciliation(
        rid, ReconciliationPayload(rows=[], groups=[], flags=flags, norm_log=norms)
    )
    assert repo.list_flags(rid) == flags  # insertion (payload) order
    assert repo.list_norm_log(rid) == norms
    assert repo.list_norm_log(rid, "ERP-0002") == [norms[0], norms[2]]


def test_payload_with_unknown_group_rejected(repo: Repo) -> None:
    run = _new_run(repo)
    before = make_payload(run.id, 1)
    repo.replace_reconciliation(run.id, before)
    stray = ReconciliationPayload(
        rows=[make_row(run.id, "ERP-0009", group_id=uuid4())],
        groups=[make_group(run.id, "GRN:9", grn_no=9)],
        flags=[],
        norm_log=[],
    )
    with pytest.raises(ValueError):
        repo.replace_reconciliation(run.id, stray)
    # a group from the previous reconcile is not a payload group either
    stale = ReconciliationPayload(
        rows=[make_row(run.id, "ERP-0009", group_id=before.groups[0].id)],
        groups=[],
        flags=[],
        norm_log=[],
    )
    with pytest.raises(ValueError):
        repo.replace_reconciliation(run.id, stale)
    other = _new_run(repo)
    foreign = ReconciliationPayload(
        rows=[make_row(other.id, "ERP-0009")], groups=[], flags=[], norm_log=[]
    )
    with pytest.raises(ValueError):  # payload records must belong to the run
        repo.replace_reconciliation(run.id, foreign)
    _assert_state(repo, run.id, before)


# --- proof -------------------------------------------------------------------------------


def test_save_proof_overwrites(repo: Repo) -> None:
    run = _new_run(repo)
    assert repo.get_proof(run.id) is None
    repo.save_proof(make_proof(run.id, computed_at=at(1)))
    latest = repo.save_proof(make_proof(run.id, computed_at=at(2)))
    assert repo.get_proof(run.id) == latest
    assert latest.computed_at == at(2)


def test_save_proof_rejects_finalised_run(repo: Repo) -> None:
    run = _new_run(repo)
    kept = repo.save_proof(make_proof(run.id, computed_at=at(1)))
    final = _finalise(repo, run, "k-final")
    with pytest.raises(StateConflict) as exc:
        repo.save_proof(make_proof(run.id, computed_at=at(2)))
    assert exc.value.current == final
    assert repo.get_proof(run.id) == kept


# --- review ------------------------------------------------------------------------------


def _resolve(repo: Repo, item_id: UUID, **over: Any) -> ReviewItem:
    args: dict[str, Any] = dict(
        status=ReviewStatus.RESOLVED,
        resolved_value="12",
        resolved_by="reviewer@example.com",
        resolved_at=at(10),
        note=None,
    )
    return repo.resolve_review_item(item_id, **(args | over))


def test_upsert_inserts_open(repo: Repo) -> None:
    run = _new_run(repo)
    item = make_review(run.id, "CELL:REG-p1-r01:qty")
    [stored] = repo.upsert_review_items(run.id, [item])
    assert stored.status is ReviewStatus.OPEN
    assert stored.item_key == item.item_key
    assert repo.get_review_item(stored.id) == stored
    assert repo.list_review_items(run.id) == [stored]


def test_upsert_keeps_decisions(repo: Repo) -> None:
    run = _new_run(repo)
    [stored] = repo.upsert_review_items(run.id, [make_review(run.id, "CELL:REG-p1-r01:qty")])
    _resolve(repo, stored.id, resolved_value="12", note="seen")
    refreshed_input = make_review(
        run.id,
        "CELL:REG-p1-r01:qty",
        reason=ReviewReason.READS_DISAGREE,
        read_a=cell("15"),
        read_b=cell("16"),
        grn_no=44,
        source_row_id="REG-p1-r02",
        field="lot_no",
        context=CellContext(page_no=1, row_index=2, note="refreshed"),
    )
    [refreshed] = repo.upsert_review_items(run.id, [refreshed_input])
    assert refreshed.id == stored.id
    assert refreshed.reason is ReviewReason.READS_DISAGREE
    assert refreshed.read_a == cell("15")
    assert refreshed.read_b == cell("16")
    assert refreshed.context == CellContext(page_no=1, row_index=2, note="refreshed")
    assert (refreshed.grn_no, refreshed.source_row_id, refreshed.field) == (
        44,
        "REG-p1-r02",
        "lot_no",
    )
    assert refreshed.status is ReviewStatus.RESOLVED
    assert refreshed.resolved_value == "12"
    assert refreshed.resolved_by == "reviewer@example.com"
    assert refreshed.resolved_at == at(10)
    assert refreshed.note == "seen"


def test_upsert_is_atomic(repo: Repo) -> None:
    """The whole batch is validated before anything is written."""
    run = _new_run(repo)
    other = _new_run(repo)
    [kept] = repo.upsert_review_items(run.id, [make_review(run.id, "CELL:REG-p1-r09:qty")])
    a = make_review(run.id, "CELL:REG-p1-r01:qty")
    b = make_review(run.id, "CELL:REG-p1-r02:qty", context=CellContext(page_no=1, row_index=2))
    unique_cases = [
        (make_review(run.id, a.item_key), "review_items_run_item_key_uq"),
        (make_review(run.id, "CELL:REG-p1-r03:qty", id=a.id), "review_items_pkey"),
        (make_review(run.id, "CELL:REG-p1-r04:qty", id=kept.id), "review_items_pkey"),
    ]
    for third, constraint in unique_cases:
        with pytest.raises(UniqueViolation) as exc:
            repo.upsert_review_items(run.id, [a, b, third])
        assert exc.value.constraint == constraint
        assert repo.list_review_items(run.id) == [kept]
    with pytest.raises(ValueError):  # item from another run
        repo.upsert_review_items(run.id, [a, b, make_review(other.id, "CELL:REG-p1-r05:qty")])
    assert repo.list_review_items(run.id) == [kept]
    assert repo.list_review_items(other.id) == []


def test_upsert_never_deletes(repo: Repo) -> None:
    run = _new_run(repo)
    a = make_review(run.id, "CELL:REG-p1-r01:qty")
    b = make_review(run.id, "CELL:REG-p1-r02:qty", context=CellContext(page_no=1, row_index=2))
    repo.upsert_review_items(run.id, [a, b])
    repo.upsert_review_items(run.id, [b])
    repo.upsert_review_items(run.id, [])
    assert {i.item_key for i in repo.list_review_items(run.id)} == {a.item_key, b.item_key}


def test_resolve_only_if_open(repo: Repo) -> None:
    run = _new_run(repo)
    [item] = repo.upsert_review_items(run.id, [make_review(run.id, "CELL:REG-p1-r01:qty")])
    with pytest.raises(ValueError):  # OPEN is not a decision
        _resolve(repo, item.id, status=ReviewStatus.OPEN)
    resolved = _resolve(repo, item.id)
    assert resolved.status is ReviewStatus.RESOLVED
    assert resolved.resolved_at == at(10)
    assert resolved == repo.get_review_item(item.id)
    with pytest.raises(StateConflict):
        _resolve(repo, item.id, status=ReviewStatus.CANNOT_DETERMINE, note="late")
    assert repo.list_review_items(run.id, ReviewStatus.OPEN) == []
    assert repo.list_review_items(run.id, ReviewStatus.RESOLVED) == [resolved]


def test_second_resolve_returns_first_decision(repo: Repo) -> None:
    run = _new_run(repo)
    [item] = repo.upsert_review_items(run.id, [make_review(run.id, "CELL:REG-p1-r01:qty")])
    first = _resolve(repo, item.id, resolved_value="12", resolved_by="a@example.com")
    with pytest.raises(StateConflict) as exc:
        _resolve(repo, item.id, resolved_value="99", resolved_by="b@example.com")
    assert exc.value.current == first
    assert repo.get_review_item(item.id) == first


def test_cannot_determine_requires_note(repo: Repo) -> None:
    run = _new_run(repo)
    [item] = repo.upsert_review_items(run.id, [make_review(run.id, "CELL:REG-p1-r01:qty")])
    for note in (None, "", "   "):
        with pytest.raises(ValueError):
            _resolve(
                repo, item.id, status=ReviewStatus.CANNOT_DETERMINE, resolved_value=None, note=note
            )
    assert repo.get_review_item(item.id).status is ReviewStatus.OPEN
    done = _resolve(
        repo, item.id, status=ReviewStatus.CANNOT_DETERMINE, resolved_value=None, note="ink smudge"
    )
    assert done.status is ReviewStatus.CANNOT_DETERMINE
    assert done.note == "ink smudge"


def test_review_list_order(repo: Repo) -> None:
    run = _new_run(repo)
    rid = run.id
    items = [
        flag_review(rid, "FLAG:SEQUENCE_GAP:5"),
        mapping_review(rid, "MAP:ERP-0001:supplier_code"),
        unmatched_review(rid, "ROW:XLS-r2"),
        make_review(rid, "CELL:REG-p2-r01:qty", context=CellContext(page_no=2, row_index=1)),
        make_review(rid, "CELL:REG-p1-r03:lot_no", context=CellContext(page_no=1, row_index=3)),
        make_review(rid, "CELL:REG-p1-r03:date", context=CellContext(page_no=1, row_index=3)),
        unmatched_review(rid, "ROW:ERP-0003"),
    ]
    repo.upsert_review_items(rid, items)
    assert [i.item_key for i in repo.list_review_items(rid)] == [
        "CELL:REG-p1-r03:date",
        "CELL:REG-p1-r03:lot_no",
        "CELL:REG-p2-r01:qty",
        "ROW:ERP-0003",
        "ROW:XLS-r2",
        "MAP:ERP-0001:supplier_code",
        "FLAG:SEQUENCE_GAP:5",
    ]
    kinds = [i.kind for i in repo.list_review_items(rid)]
    assert kinds[0] is ReviewKind.CELL and kinds[-1] is ReviewKind.FLAG_ACK


# --- notifications -----------------------------------------------------------------------


def test_insert_notification_idempotent(repo: Repo) -> None:
    run = _new_run(repo)
    n = make_notification(run.id, idempotency_key="SUMMARY:k")
    stored, created = repo.insert_notification(n)
    assert created is True
    again, created2 = repo.insert_notification(
        make_notification(run.id, idempotency_key="SUMMARY:k")
    )
    assert created2 is False
    assert again == stored
    assert repo.list_notifications(run.id) == [stored]
    assert repo.get_notification_by_key("SUMMARY:k") == stored
    assert repo.get_notification_by_key("SUMMARY:none") is None


def test_update_notification_compare_and_set(repo: Repo) -> None:
    run = _new_run(repo)
    n, _ = repo.insert_notification(make_notification(run.id))
    sending = repo.update_notification(
        n.id,
        expected={NotificationStatus.PENDING},
        status=NotificationStatus.SENDING,
        attempts=1,
        updated_at=at(5),
    )
    assert sending.status is NotificationStatus.SENDING
    assert sending.updated_at == at(5)
    with pytest.raises(StateConflict) as exc:
        repo.update_notification(
            n.id,
            expected={NotificationStatus.PENDING},
            status=NotificationStatus.SENDING,
            updated_at=at(6),
        )
    assert exc.value.current == sending
    bad_fields: list[dict[str, Any]] = [
        {"id": uuid4()},
        {"run_id": uuid4()},
        {"kind": "ALERT"},
        {"idempotency_key": "other"},
    ]
    for bad in bad_fields:
        with pytest.raises(ValueError):
            repo.update_notification(
                n.id, expected={NotificationStatus.SENDING}, updated_at=at(6), **bad
            )
    assert repo.get_notification_by_key(n.idempotency_key) == sending
    with pytest.raises(ValueError):  # updated_at is always set by the caller
        repo.update_notification(
            n.id, expected={NotificationStatus.SENDING}, status=NotificationStatus.SENT
        )
    sent = repo.update_notification(
        n.id,
        expected={NotificationStatus.SENDING},
        status=NotificationStatus.SENT,
        telegram_message_id=77,
        sent_at=at(7),
        updated_at=at(7),
    )
    assert repo.get_notification_by_key(n.idempotency_key) == sent
    assert sent.telegram_message_id == 77


def test_list_stale_sending(repo: Repo) -> None:
    run = _new_run(repo)
    old, _ = repo.insert_notification(
        make_notification(run.id, status=NotificationStatus.SENDING, updated_at=at(1))
    )
    repo.insert_notification(
        make_notification(run.id, status=NotificationStatus.SENDING, updated_at=at(10))
    )
    repo.insert_notification(
        make_notification(run.id, status=NotificationStatus.PENDING, updated_at=at(1))
    )
    boundary, _ = repo.insert_notification(
        make_notification(run.id, status=NotificationStatus.SENDING, updated_at=at(5))
    )
    assert repo.list_stale_sending(at(5)) == [old]  # strictly older than
    assert repo.list_stale_sending(at(6)) == [old, boundary]


def test_repo_contract_get_latest_notification(repo: Repo) -> None:
    """004 E2: latest of a kind by updated_at, then id; None when there is none."""
    assert repo.get_latest_notification(NotificationKind.ALERT) is None
    run = _new_run(repo)
    alert = NotificationKind.ALERT
    ids = sorted([uuid4(), uuid4()])
    repo.insert_notification(make_notification(run.id, kind=alert, updated_at=at(1)))
    low, _ = repo.insert_notification(
        make_notification(run.id, id=ids[0], kind=alert, updated_at=at(3))
    )
    high, _ = repo.insert_notification(
        make_notification(run.id, id=ids[1], kind=alert, updated_at=at(3))
    )
    repo.insert_notification(make_notification(run.id, updated_at=at(9)))  # SUMMARY
    assert repo.get_latest_notification(alert) == high  # tie on updated_at: larger id
    failed = repo.update_notification(
        low.id,
        expected={NotificationStatus.PENDING},
        status=NotificationStatus.FAILED,
        updated_at=at(4),
    )
    assert repo.get_latest_notification(alert) == failed
    assert repo.get_latest_notification(NotificationKind.SUMMARY) is not None


# --- run events and audit ----------------------------------------------------------------


def test_run_events_newest_first(repo: Repo) -> None:
    run = _new_run(repo)
    other = _new_run(repo)
    e1 = repo.add_run_event(make_event(run.id, at=at(1)))
    e3 = repo.add_run_event(make_event(run.id, at=at(3)))
    t1 = repo.add_run_event(make_event(run.id, at=at(2), stage=Stage.VALIDATE))
    t2 = repo.add_run_event(make_event(run.id, at=at(2), stage=Stage.EXTRACT))
    repo.add_run_event(make_event(other.id, at=at(9)))
    # at desc, then newest insertion first for equal `at` (C27 seq; a frozen clock ties all)
    assert [e.id for e in repo.list_run_events(run.id)] == [e3.id, t2.id, t1.id, e1.id]


def test_audit_append_assigns_increasing_ids(repo: Repo) -> None:
    ids = [repo.append_audit(make_audit(id=0, at=at(i))).id for i in range(3)]
    assert ids == sorted(set(ids))
    assert len(ids) == 3
    assert [e.id for e in repo.list_audit()] == list(reversed(ids))


def test_audit_has_no_update_or_delete(repo: Repo) -> None:
    audit_api = {n for n in dir(repo) if "audit" in n and not n.startswith("_")}
    assert audit_api == {"append_audit", "list_audit"}
    entry = repo.append_audit(make_audit(after={"x": 1}))
    assert entry.after is not None
    entry.after["x"] = 2
    listed = repo.list_audit()
    assert listed[0].after is not None
    listed[0].after["x"] = 3
    listed.clear()
    assert repo.list_audit()[0].after == {"x": 1}


def test_list_audit_filters(repo: Repo) -> None:
    run = _new_run(repo)
    other = _new_run(repo)
    a = repo.append_audit(make_audit(at=at(1), run_id=run.id, actor="a@example.com"))
    b = repo.append_audit(make_audit(at=at(2), run_id=run.id, actor="system"))
    c = repo.append_audit(
        make_audit(
            at=at(3), run_id=other.id, actor="a@example.com", action=AuditAction.RUN_FINALISED
        )
    )
    d = repo.append_audit(make_audit(at=at(4), run_id=None, actor="a@example.com"))
    assert [e.id for e in repo.list_audit()] == [d.id, c.id, b.id, a.id]
    assert [e.id for e in repo.list_audit(run_id=run.id)] == [b.id, a.id]
    assert [e.id for e in repo.list_audit(actor="a@example.com")] == [d.id, c.id, a.id]
    assert [e.id for e in repo.list_audit(run_id=run.id, actor="a@example.com")] == [a.id]


# --- MemoryRepo internals (not part of the shared contract) ------------------------------


def test_memory_audit_log_is_append_only() -> None:
    from app.core.memory_repo import MemoryRepo

    mem = MemoryRepo()
    mem.append_audit(make_audit())
    log = mem._audit  # reaching in on purpose
    with pytest.raises(AppendOnlyViolation):
        log[0] = make_audit(id=1)
    with pytest.raises(AppendOnlyViolation):
        del log[0]
    with pytest.raises(AppendOnlyViolation):
        log.clear()
    assert len(mem.list_audit()) == 1


def test_memory_repo_calls_are_serialised() -> None:
    """MemoryRepo behind the threadpool: every call holds one re-entrant lock, so a write
    waits while another thread holds it (no torn dict updates or iteration errors)."""
    import threading

    from app.core.memory_repo import MemoryRepo

    repo = MemoryRepo()
    done = threading.Event()

    def write() -> None:
        repo.create_run(make_run())
        done.set()

    with repo._lock:
        worker = threading.Thread(target=write)
        worker.start()
        assert not done.wait(0.05)  # blocked while the lock is held elsewhere
    worker.join(5)
    assert done.is_set()
