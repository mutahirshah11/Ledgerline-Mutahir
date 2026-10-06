"""Identifier formats match data-model.md Section 3 (FR-012, FR-013)."""

import hashlib
from typing import Any
from uuid import UUID

import pytest

from app.domain import ids
from app.domain.enums import FlagType, ReviewKind, Stage

RUN_ID = UUID("7f3c0000-0000-4000-8000-000000000001")


def test_register_row_id_format() -> None:
    assert ids.register_row_id(2, 14) == "REG-p2-r14"
    assert ids.register_row_id(1, 3) == "REG-p1-r03"
    assert ids.register_row_id(12, 16) == "REG-p12-r16"


def test_erp_row_id_format() -> None:
    assert ids.erp_row_id(27) == "ERP-0027"
    assert ids.erp_row_id(1) == "ERP-0001"


def test_sheet_row_id_format() -> None:
    assert ids.sheet_row_id(30) == "XLS-r30"
    assert ids.sheet_row_id(2) == "XLS-r2"


def test_group_keys() -> None:
    assert ids.grn_group_key(4425) == "GRN:4425"
    assert ids.lot_group_key("L2609-002", "S-102") == "LOT:L2609-002|S-102"
    assert ids.row_group_key("XLS-r10") == "ROW:XLS-r10"


def test_review_item_keys() -> None:
    row = "REG-p2-r14"
    assert ids.review_item_key(ReviewKind.CELL, row_id=row, field="qty") == "CELL:REG-p2-r14:qty"
    assert (
        ids.review_item_key(ReviewKind.UNKNOWN_MAPPING, row_id="ERP-0027", field="supplier")
        == "MAP:ERP-0027:supplier"
    )
    assert ids.review_item_key(ReviewKind.UNMATCHED_ROW, row_id="XLS-r30") == "ROW:XLS-r30"
    assert (
        ids.review_item_key(ReviewKind.FLAG_ACK, flag=FlagType.BACKDATED_ENTRY, row_id="ERP-0027")
        == "FLAG:BACKDATED_ENTRY:ERP-0027"
    )
    assert (
        ids.review_item_key(ReviewKind.FLAG_ACK, flag=FlagType.SEQUENCE_GAP, grn_no=4426)
        == "FLAG:SEQUENCE_GAP:4426"
    )


def test_summary_and_alert_keys() -> None:
    assert ids.summary_key("abc123") == "SUMMARY:abc123"
    assert ids.alert_key(RUN_ID, Stage.EXTRACT, 1) == f"ALERT:{RUN_ID}:extract:1"


def test_run_key_is_order_independent() -> None:
    hashes = ["c" * 64, "a" * 64, "b" * 64]
    expected = hashlib.sha256(("2026-09" + "a" * 64 + "b" * 64 + "c" * 64).encode()).hexdigest()
    assert ids.run_key("2026-09", hashes) == expected
    assert ids.run_key("2026-09", list(reversed(hashes))) == expected
    assert ids.run_key("2026-10", hashes) != expected


@pytest.mark.parametrize(
    ("fn", "args"),
    [
        (ids.register_row_id, (2, 0)),
        (ids.register_row_id, (2, 17)),
        (ids.register_row_id, (0, 1)),
        (ids.register_row_id, (-1, 1)),
        (ids.erp_row_id, (0,)),
        (ids.erp_row_id, (-3,)),
        (ids.sheet_row_id, (1,)),
        (ids.sheet_row_id, (-2,)),
        (ids.grn_group_key, (0,)),
        (ids.grn_group_key, (-5,)),
        (ids.erp_row_id, (True,)),
        (ids.lot_group_key, ("", "S-102")),
        (ids.lot_group_key, ("L2609-002", "")),
        (ids.run_key, ("2026-13", ["a" * 64])),
        (ids.run_key, ("2026-09", "a" * 64)),
        (ids.run_key, ("2026-09", [])),
        (ids.run_key, ("2026-09", ["A" * 64])),
        (ids.run_key, ("2026-09", ["a" * 63])),
        (ids.lot_group_key, ("   ", "S-102")),
        (ids.lot_group_key, ("L2609-002", " ")),
        (ids.summary_key, ("  ",)),
    ],
)
def test_ids_reject_invalid_inputs(fn: Any, args: tuple[Any, ...]) -> None:
    with pytest.raises(ValueError):
        fn(*args)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": ReviewKind.CELL, "row_id": "REG-p2-r14"},
        {"kind": ReviewKind.UNKNOWN_MAPPING, "field": "unit"},
        {"kind": ReviewKind.UNMATCHED_ROW},
        {"kind": ReviewKind.FLAG_ACK, "row_id": "ERP-0001"},
        {"kind": ReviewKind.FLAG_ACK, "flag": FlagType.SEQUENCE_GAP},
        {"kind": ReviewKind.CELL, "row_id": " ", "field": "qty"},
        {"kind": ReviewKind.FLAG_ACK, "flag": FlagType.SEQUENCE_GAP, "row_id": "", "grn_no": 5},
    ],
)
def test_review_item_key_rejects_missing_parts(kwargs: dict[str, Any]) -> None:
    kind = kwargs.pop("kind")
    with pytest.raises(ValueError):
        ids.review_item_key(kind, **kwargs)
