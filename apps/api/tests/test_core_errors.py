"""Domain errors carry the context the contract promises (repo-interface G4 to G7)."""

import pytest

from app.core.errors import (
    AppendOnlyViolation,
    LedgerlineError,
    NotFound,
    StateConflict,
    UniqueViolation,
    UpstreamFailed,
)


def test_not_found_carries_entity_and_id() -> None:
    err = NotFound("run", "abc")
    assert (err.entity, err.id) == ("run", "abc")
    assert "run" in str(err) and "abc" in str(err)


def test_unique_violation_carries_constraint() -> None:
    assert UniqueViolation("source_files_run_sha").constraint == "source_files_run_sha"


def test_state_conflict_carries_current_record() -> None:
    current = {"status": "FINALISED"}
    assert StateConflict(current).current is current


def test_upstream_failed_carries_service_and_detail() -> None:
    err = UpstreamFailed("supabase", "timeout")
    assert (err.service, err.detail) == ("supabase", "timeout")


@pytest.mark.parametrize(
    "err",
    [
        NotFound("a", "b"),
        UniqueViolation("c"),
        StateConflict(None),
        AppendOnlyViolation("audit_log"),
        UpstreamFailed("s", "d"),
    ],
)
def test_all_errors_share_base(err: Exception) -> None:
    assert isinstance(err, LedgerlineError)
