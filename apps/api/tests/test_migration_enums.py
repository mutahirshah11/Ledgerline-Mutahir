"""Postgres enum types in 0001_init.sql equal the Python enums (data-model.md Section 2, T026).

contracts/migration-0001.md item 2: 16 enum types; values and order exactly as Section 2.
"""

from enum import StrEnum

import pytest

from app.domain import enums as e
from tests.sql_helpers import enum_types

PG_ENUMS: dict[str, type[StrEnum]] = {
    "source": e.Source,
    "cell_status": e.CellStatus,
    "row_kind": e.RowKind,
    "outcome": e.Outcome,
    "flag_type": e.FlagType,
    "review_kind": e.ReviewKind,
    "review_status": e.ReviewStatus,
    "run_status": e.RunStatus,
    "proof_status": e.ProofStatus,
    "notification_kind": e.NotificationKind,
    "notification_status": e.NotificationStatus,
    "norm_code": e.NormCode,
    "role": e.Role,
    "audit_channel": e.AuditChannel,
    "run_event_status": e.RunEventStatus,
    "read_variant": e.ReadVariant,
}


def test_exactly_sixteen_enum_types() -> None:
    assert len(PG_ENUMS) == 16
    assert set(enum_types()) == set(PG_ENUMS)


def test_python_only_code_lists_are_not_postgres_types() -> None:
    # Section 2.3 lists (incl. Stage, blueprint C9) are text columns, never enum types
    found = set(enum_types())
    for name in ("stage", "review_reason", "review_action", "audit_action", "error_code"):
        assert name not in found


@pytest.mark.parametrize("pg_name", sorted(PG_ENUMS))
def test_enum_values_match(pg_name: str) -> None:
    assert enum_types()[pg_name] == [m.value for m in PG_ENUMS[pg_name]]
