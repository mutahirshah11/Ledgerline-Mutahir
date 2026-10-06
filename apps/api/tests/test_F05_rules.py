"""F-05 rules on hand-built canonical rows (contracts/reconcile.md R1 to R11, data-model 4, 5)."""

import datetime as dt
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.domain.enums import CellStatus, ComparedField, Outcome, ReviewReason, RowKind, Source
from app.domain.models import CanonicalRow, Group
from app.pipeline.reconcile import ReconcileResult, reconcile
from tests.factories import make_row

RUN = UUID("00000000-0000-4000-8000-0000000000f5")


def _row(row_id: str, grn: int | None = 4401, **over: Any) -> CanonicalRow:
    """A clear receipt row; same defaults for every source, so a trio matches."""
    base: dict[str, Any] = dict(
        grn_no=grn,
        date=dt.date(2026, 9, 1),
        supplier_code="S-101",
        item="Polybags",
        lot_no="L2609-001",
        qty=Decimal("10"),
        uom="KG",
        qty_base=Decimal("10.00"),
        base_uom="KG",
    )
    return make_row(RUN, row_id, **(base | over))


def _trio(grn: int = 4401, n: int = 1, **over: Any) -> list[CanonicalRow]:
    return [
        _row(f"REG-p1-r{n:02}", grn, **over),
        _row(f"ERP-{n:04}", grn, **over),
        _row(f"XLS-r{n + 1}", grn, **over),
    ]


def _group(result: ReconcileResult, key: str) -> Group:
    return next(g for g in result.groups if g.group_key == key)


def _row_out(result: ReconcileResult, row_id: str) -> CanonicalRow:
    return next(r for r in result.rows if r.source_row_id == row_id)


def test_F05_R4_fallback_joins_single_group() -> None:
    rows = [
        _row("REG-p1-r01", 4401),
        _row("ERP-0001", 4401),
        _row("REG-p1-r02", 4402, lot_no="L2609-002"),
        _row("XLS-r9", None),  # no GRN; lot/supplier match only group 4401
    ]
    result = reconcile(RUN, rows)

    group = _group(result, "GRN:4401")
    assert group.row_ids == {
        Source.REGISTER: ["REG-p1-r01"],
        Source.ERP: ["ERP-0001"],
        Source.SHEET: ["XLS-r9"],
    }
    assert _row_out(result, "XLS-r9").group_id == group.id
    assert group.outcome is Outcome.MATCHED
    assert result.review_items == []


def test_F05_R4_unmatched_no_key_review_item() -> None:
    rows = [
        *_trio(4401, 1),
        *_trio(4402, 2),  # same lot and supplier as 4401: two candidates
        _row("XLS-r9", None),
        _row("XLS-r10", None, lot_no=None),
    ]
    result = reconcile(RUN, rows)

    lot = _group(result, "LOT:L2609-001|S-101")
    assert lot.grn_no is None
    assert lot.outcome is Outcome.UNMATCHED_NO_KEY
    assert lot.row_ids == {Source.SHEET: ["XLS-r9"]}
    no_lot = _group(result, "ROW:XLS-r10")
    assert no_lot.outcome is Outcome.UNMATCHED_NO_KEY
    assert _row_out(result, "XLS-r9").outcome is Outcome.UNMATCHED_NO_KEY

    items = {i.item_key: i for i in result.review_items}
    assert sorted(items) == ["ROW:XLS-r10", "ROW:XLS-r9"]
    item = items["ROW:XLS-r9"]
    assert item.reason is ReviewReason.UNMATCHED_NO_KEY
    assert item.source_row_id == "XLS-r9"
    assert item.context.model_dump() == {"group_key": "LOT:L2609-001|S-101", "other_rows": []}


def test_F05_R11_group_ids_distinct_from_review_item_ids() -> None:
    rows = [*_trio(4401), _row("XLS-r10", None, lot_no=None)]  # fallback group ROW:XLS-r10
    result = reconcile(RUN, rows)

    assert _group(result, "ROW:XLS-r10")
    assert {i.item_key for i in result.review_items} == {"ROW:XLS-r10"}
    group_ids = {g.id for g in result.groups}
    assert group_ids.isdisjoint({i.id for i in result.review_items})


def test_F05_R11_review_items_and_other_rows_in_file_order() -> None:
    rows = [
        _row("REG-p1-r01"),
        _row("ERP-0001"),
        _row("XLS-r2"),
        _row("XLS-r10", qty=Decimal("12"), qty_base=Decimal("12.00")),  # conflicts with r2
        _row("XLS-r8", None, lot_no=None),  # own fallback group
    ]
    result = reconcile(RUN, rows)

    assert [i.item_key for i in result.review_items] == ["ROW:XLS-r8", "ROW:XLS-r10"]
    conflict = result.review_items[1]
    assert conflict.context.model_dump()["other_rows"] == ["REG-p1-r01", "ERP-0001", "XLS-r2"]


def test_F05_R5_conflicting_rows_in_source() -> None:
    rows = [*_trio(4401), _row("ERP-0002", 4401, qty=Decimal("12"), qty_base=Decimal("12.00"))]
    result = reconcile(RUN, rows)

    group = _group(result, "GRN:4401")
    assert group.outcome is Outcome.NEEDS_REVIEW
    assert group.row_ids[Source.ERP] == ["ERP-0001", "ERP-0002"]
    assert _row_out(result, "ERP-0002").row_kind is RowKind.RECEIPT
    [item] = result.review_items
    assert item.item_key == "ROW:ERP-0002"
    assert item.reason is ReviewReason.CONFLICTING_ROWS_IN_SOURCE
    assert item.grn_no == 4401


def test_F05_R8_null_not_a_difference() -> None:
    """GE-09: register qty ILLEGIBLE (null) before review; ERP and sheet agree on 250."""
    qty: dict[str, Any] = dict(
        qty=Decimal("250"), uom="PCS", qty_base=Decimal("250.00"), base_uom="PCS"
    )
    reg = _row("REG-p2-r14", 4428, **qty)
    reg = reg.model_copy(
        update={
            "qty": None,
            "qty_base": None,
            "cell_status": reg.cell_status | {"qty": CellStatus.ILLEGIBLE},
        }
    )
    rows = [reg, _row("ERP-0027", 4428, **qty), _row("XLS-r30", 4428, **qty)]
    result = reconcile(RUN, rows)

    group = _group(result, "GRN:4428")
    assert group.outcome is Outcome.NEEDS_REVIEW
    assert group.diffs == []


def test_F05_R8_all_three_differ_outlier_null() -> None:
    rows = [
        _row("REG-p1-r01", lot_no="L2609-001"),
        _row("ERP-0001", lot_no="L2609-002"),
        _row("XLS-r2", lot_no="L2609-003"),
    ]
    group = _group(reconcile(RUN, rows), "GRN:4401")

    assert group.outcome is Outcome.MISMATCHED
    [diff] = group.diffs
    assert diff.field is ComparedField.LOT_NO
    assert diff.outlier is None
    assert diff.values == {
        Source.REGISTER: "L2609-001",
        Source.ERP: "L2609-002",
        Source.SHEET: "L2609-003",
    }


def test_F05_R6_base_unit_diff_is_uom() -> None:
    ctn: dict[str, Any] = dict(
        qty=Decimal("48"), uom="CTN", qty_base=Decimal("48.00"), base_uom="CTN"
    )
    pcs: dict[str, Any] = dict(
        qty=Decimal("48"), uom="PCS", qty_base=Decimal("48.00"), base_uom="PCS"
    )
    rows = [_row("REG-p1-r01", **ctn), _row("ERP-0001", **pcs), _row("XLS-r2", **ctn)]
    group = _group(reconcile(RUN, rows), "GRN:4401")

    assert group.outcome is Outcome.MISMATCHED
    [diff] = group.diffs
    assert diff.field is ComparedField.UOM
    assert diff.outlier is Source.ERP
    assert diff.values == {Source.REGISTER: "CTN", Source.ERP: "PCS", Source.SHEET: "CTN"}


def test_F05_R7_matched_after_conversion() -> None:
    yd: dict[str, Any] = dict(
        qty=Decimal("1200"), uom="YD", qty_base=Decimal("1097.28"), base_uom="M"
    )
    m: dict[str, Any] = dict(
        qty=Decimal("1097.28"), uom="M", qty_base=Decimal("1097.28"), base_uom="M"
    )
    rows = [_row("REG-p1-r01", **yd), _row("ERP-0001", **m), _row("XLS-r2", **m)]
    group = _group(reconcile(RUN, rows), "GRN:4401")

    assert group.outcome is Outcome.MATCHED_AFTER_CONVERSION
    assert group.diffs == []


def test_F05_R7_corrected_needs_review() -> None:
    """GE-06: register qty CORRECTED and unresolved (value null, F-04.R9); ERP 66, sheet 60."""
    reg = _row("REG-p2-r05", 4419, qty=None, qty_base=None)
    reg = reg.model_copy(update={"cell_status": reg.cell_status | {"qty": CellStatus.CORRECTED}})
    rows = [
        reg,
        _row("ERP-0019", 4419, qty=Decimal("66"), qty_base=Decimal("66.00")),
        _row("XLS-r21", 4419, qty=Decimal("60"), qty_base=Decimal("60.00")),
    ]
    result = reconcile(RUN, rows)

    group = _group(result, "GRN:4419")
    assert group.outcome is Outcome.NEEDS_REVIEW
    assert {r.outcome for r in result.rows} == {Outcome.NEEDS_REVIEW}
    [diff] = group.diffs
    assert diff.field is ComparedField.QTY
    assert diff.outlier is None  # only two known values
    assert diff.compared == {
        Source.REGISTER: None,
        Source.ERP: "66.00 KG",
        Source.SHEET: "60.00 KG",
    }


def test_F05_not_a_problem_cone_ctn() -> None:
    """4404 in CONE and 4417 in CTN: each consistent, so never a uom diff."""
    cone: dict[str, Any] = dict(
        qty=Decimal("40"), uom="CONE", qty_base=Decimal("40.00"), base_uom="CONE"
    )
    ctn: dict[str, Any] = dict(
        qty=Decimal("48"), uom="CTN", qty_base=Decimal("48.00"), base_uom="CTN"
    )
    rows = [
        *_trio(4404, 4, **cone),
        _row("REG-p2-r03", 4417, **ctn),
        _row("ERP-0017", 4417, **(ctn | {"qty": Decimal("46"), "qty_base": Decimal("46.00")})),
        _row("XLS-r19", 4417, **ctn),
    ]
    result = reconcile(RUN, rows)

    assert _group(result, "GRN:4404").outcome is Outcome.MATCHED
    g4417 = _group(result, "GRN:4417")
    assert [(d.field, d.outlier) for d in g4417.diffs] == [(ComparedField.QTY, Source.ERP)]


def test_F05_invariants() -> None:
    rows = [
        *_trio(4401, 1),
        _row("XLS-r3", 4401),  # identical to XLS-r2: duplicate
        _row("REG-p1-r02", 4402, lot_no="L2609-002"),
        _row("XLS-r4", None, qty=Decimal("-5"), qty_base=Decimal("-5.00")),  # return
        _row("XLS-r5", None, lot_no="L2609-009"),  # no key
    ]
    before = [r.model_dump() for r in rows]
    result = reconcile(RUN, rows)

    assert [r.model_dump() for r in rows] == before
    assert sorted(r.source_row_id for r in result.rows) == sorted(r.source_row_id for r in rows)
    assert all(r.outcome is not None for r in result.rows)
    assert not any(out is inp for out in result.rows for inp in rows)

    by_group: dict[UUID, set[str]] = {}
    for r in result.rows:
        if r.row_kind is RowKind.RECEIPT:
            assert r.group_id is not None
            by_group.setdefault(r.group_id, set()).add(r.source_row_id)
        else:
            assert r.group_id is None
    for g in result.groups:
        assert {i for ids in g.row_ids.values() for i in ids} == by_group.pop(g.id)
        assert bool(g.missing_from) == (g.outcome is Outcome.MISSING)
    assert by_group == {}

    assert _row_out(result, "XLS-r3").outcome is Outcome.DUPLICATE
    assert _row_out(result, "XLS-r3").duplicate_of == "XLS-r2"
    assert _row_out(result, "XLS-r4").outcome is Outcome.NON_RECEIPT
    assert _group(result, "GRN:4402").missing_from == [Source.ERP, Source.SHEET]
