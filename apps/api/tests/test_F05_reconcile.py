"""F-05 acceptance criteria AC1 to AC9 on the normalised 2026-09 pack (blueprint Sections 8, 17)."""

import datetime as dt
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.domain.enums import ComparedField, Outcome, RowKind, Source
from app.domain.models import CanonicalRow, Group
from app.pipeline.reconcile import ReconcileResult, reconcile
from tests.conftest import PACK_RUN_ID
from tests.support.pack import build_engine_inputs

R, E, S = Source.REGISTER, Source.ERP, Source.SHEET


@pytest.fixture(scope="module")
def pack_rows(pack_dir: Path, ground_truth_dir: Path) -> list[CanonicalRow]:
    """The 86 canonical rows of the pack before review (F-04), normalised once per module."""
    from app.pipeline.normalise import normalise_all, read_erp, read_sheet

    inputs = build_engine_inputs(pack_dir, ground_truth_dir, PACK_RUN_ID)
    return normalise_all(
        PACK_RUN_ID,
        inputs.month,
        inputs.register_pages,
        read_erp(inputs.erp_bytes),
        read_sheet(inputs.sheet_bytes, inputs.month),
        inputs.mappings,
        inputs.decisions,
    ).rows


@pytest.fixture(scope="module")
def result(pack_rows: list[CanonicalRow]) -> ReconcileResult:
    return reconcile(PACK_RUN_ID, pack_rows)


def _group(result: ReconcileResult, grn: int) -> Group:
    return next(g for g in result.groups if g.grn_no == grn)


def _row(result: ReconcileResult, row_id: str) -> CanonicalRow:
    return next(r for r in result.rows if r.source_row_id == row_id)


def test_F05_AC1_counts_before_review(result: ReconcileResult) -> None:
    groups = Counter(g.outcome for g in result.groups)
    assert groups == {
        Outcome.MATCHED: 21,
        Outcome.MISMATCHED: 4,
        Outcome.NEEDS_REVIEW: 2,
        Outcome.MISSING: 2,
    }
    rows = Counter((r.outcome, r.source) for r in result.rows)
    expected = {
        Outcome.MATCHED: (21, 21, 21),
        Outcome.MISMATCHED: (4, 4, 4),
        Outcome.NEEDS_REVIEW: (2, 2, 2),
        Outcome.MISSING: (1, 1, 1),
        Outcome.DUPLICATE: (0, 0, 1),
        Outcome.NON_RECEIPT: (0, 0, 1),
    }
    assert rows == {
        (outcome, source): n
        for outcome, counts in expected.items()
        for source, n in zip((R, E, S), counts, strict=True)
        if n
    }
    assert Counter(r.source for r in result.rows) == {R: 28, E: 28, S: 30}


@pytest.mark.parametrize(
    ("grn", "field"),
    [(4412, ComparedField.LOT_NO), (4415, ComparedField.DATE), (4417, ComparedField.QTY)],
)
def test_F05_AC2_diff_and_outlier(result: ReconcileResult, grn: int, field: ComparedField) -> None:
    group = _group(result, grn)
    assert group.outcome is Outcome.MISMATCHED
    assert [(d.field, d.outlier) for d in group.diffs] == [(field, E)]


def test_F05_AC3_missing_from_erp(result: ReconcileResult) -> None:
    group = _group(result, 4421)
    assert group.outcome is Outcome.MISSING
    assert group.missing_from == [E]


def test_F05_AC4_missing_from_register_sheet(result: ReconcileResult) -> None:
    group = _group(result, 4430)
    assert group.outcome is Outcome.MISSING
    assert group.missing_from == [R, S]
    assert group.row_ids == {E: ["ERP-0028"]}


def test_F05_AC5_duplicate_4409(result: ReconcileResult) -> None:
    dup = _row(result, "XLS-r11")
    assert (dup.row_kind, dup.outcome) == (RowKind.DUPLICATE, Outcome.DUPLICATE)
    assert dup.duplicate_of == "XLS-r10"
    assert dup.group_id is None
    group = _group(result, 4409)
    assert group.outcome is Outcome.MATCHED
    assert group.row_ids[S] == ["XLS-r10"]


def test_F05_AC6_return_non_receipt(result: ReconcileResult) -> None:
    row = _row(result, "XLS-r31")
    assert (row.row_kind, row.outcome) == (RowKind.NON_RECEIPT, Outcome.NON_RECEIPT)
    assert row.group_id is None
    assert all("XLS-r31" not in g.row_ids.get(S, []) for g in result.groups)


def test_F05_AC7_units_4425(result: ReconcileResult) -> None:
    group = _group(result, 4425)
    assert group.outcome is Outcome.MISMATCHED
    [diff] = group.diffs
    assert (diff.field, diff.outlier) == (ComparedField.QTY, S)
    assert diff.compared == {R: "1097.28 M", E: "1097.28 M", S: "1200.00 M"}


def _dump(result: ReconcileResult) -> str:
    return json.dumps(
        {
            "rows": [r.model_dump(mode="json") for r in result.rows],
            "groups": [g.model_dump(mode="json") for g in result.groups],
            "review_items": [i.model_dump(mode="json") for i in result.review_items],
        },
        sort_keys=True,
    )


def test_F05_AC8_deterministic(pack_rows: list[CanonicalRow], result: ReconcileResult) -> None:
    again = reconcile(PACK_RUN_ID, list(reversed(pack_rows)))
    assert _dump(again) == _dump(result)


_CHANGES: dict[ComparedField, dict[str, Any]] = {
    ComparedField.DATE: {"date": dt.date(2026, 9, 30)},
    ComparedField.SUPPLIER_CODE: {"supplier_code": "S-107"},
    ComparedField.LOT_NO: {"lot_no": "L2609-999"},
    ComparedField.QTY: {"qty": Decimal("999"), "qty_base": Decimal("999.00")},
}


@pytest.mark.parametrize("field", list(_CHANGES))
def test_F05_AC9_change_field_mismatches(
    pack_rows: list[CanonicalRow], result: ReconcileResult, field: ComparedField
) -> None:
    assert _group(result, 4401).outcome is Outcome.MATCHED
    changed = [
        r.model_copy(update=_CHANGES[field]) if r.grn_no == 4401 and r.source is E else r
        for r in pack_rows
    ]
    group = _group(reconcile(PACK_RUN_ID, changed), 4401)
    assert group.outcome is Outcome.MISMATCHED
    assert [(d.field, d.outlier) for d in group.diffs] == [(field, E)]
