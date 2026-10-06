"""F-07 rule tests on hand-built inputs (contracts/proof.md; blueprint Section 19, C23)."""

import ast
import io
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import openpyxl
import pytest

import app.pipeline.proof as proof
from app.domain.enums import (
    PRINTED_ROW_SLOTS,
    CellStatus,
    FlagType,
    Outcome,
    ReadVariant,
    RegisterField,
    ReviewReason,
    ReviewStatus,
    RowKind,
    Source,
)
from app.domain.models import (
    CanonicalRow,
    Cell,
    ControlFlag,
    Group,
    MergedCell,
    MergedRow,
    RegisterPageInput,
    ReviewItem,
    RowInOneReadContext,
)
from app.pipeline.engine import EngineInputs
from app.pipeline.normalise import load_mappings
from tests.factories import make_flag, make_group, make_review, make_row

RUN = UUID("00000000-0000-4000-8000-000000000f07")
ERP_HEADER = (
    "grn_no,grn_date,supplier_code,supplier_name,item_desc,lot_no,qty,uom,entered_by,entered_at"
)
SHEET_HEADER = ("Date", "GRN", "Supplier", "Material", "Lot", "Qty Received", "UOM", "Remarks")


def _cell(value: str | None) -> Cell:
    return Cell(value=value, status=CellStatus.CLEAR)


def _merged_row(
    row_index: int, qty: str, unit: str, *, in_a: bool = True, in_b: bool = True
) -> MergedRow:
    values = {f: "x" for f in RegisterField} | {RegisterField.QTY: qty, RegisterField.UNIT: unit}
    cells = {
        f: MergedCell(
            merged=_cell(v),
            read_a=_cell(v) if in_a else None,
            read_b=_cell(v) if in_b else None,
            review_reason=None,
        )
        for f, v in values.items()
    }
    return MergedRow(page_no=1, row_index=row_index, cells=cells)


def _page(
    rows: list[MergedRow], *, count_a: int, count_b: int, blank_a: int, blank_b: int
) -> RegisterPageInput:
    return RegisterPageInput(
        page_no=1,
        month_written="Sep 2026",
        row_count_a=count_a,
        row_count_b=count_b,
        blank_slots_a=blank_a,
        blank_slots_b=blank_b,
        rows=rows,
    )


def _full_page(rows: list[MergedRow]) -> RegisterPageInput:
    n = len(rows)
    blank = PRINTED_ROW_SLOTS - n
    return _page(rows, count_a=n, count_b=n, blank_a=blank, blank_b=blank)


def _sheet_bytes(rows: list[tuple[object, object]]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Sep-2026"
    ws.append(SHEET_HEADER)
    for qty, uom in rows:
        ws.append([None, None, None, None, None, qty, uom, None])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _inputs(
    pages: Sequence[RegisterPageInput] = (),
    *,
    erp: Sequence[str] = (),
    sheet: Sequence[tuple[object, object]] = (),
    decisions: Sequence[ReviewItem] = (),
) -> EngineInputs:
    return EngineInputs(
        run_id=RUN,
        month="2026-09",
        register_pages=list(pages),
        erp_bytes=("\n".join([ERP_HEADER, *erp]) + "\n").encode("utf-8-sig"),
        sheet_bytes=_sheet_bytes(list(sheet)),
        mappings=load_mappings(),
        decisions=list(decisions),
    )


def _placed(row_id: str, qty: str | None, unit_key: str, unit: str, **over: object) -> CanonicalRow:
    base: dict[str, object] = dict(
        qty=None if qty is None else Decimal(qty),
        raw={unit_key: unit, "qty": qty},
        outcome=Outcome.MATCHED,
    )
    return make_row(RUN, row_id, **(base | over))


def _erp_line(qty: str, uom: str) -> str:
    return f"GRN-1,2026-09-01,S-101,Q,ITEM,L1,{qty},{uom},clerk,2026-09-01 10:00"


# --- C4 -------------------------------------------------------------------------------------


def test_F07_C4_keys_by_unit_as_recorded() -> None:
    inputs = _inputs([_full_page([_merged_row(1, "1200", "yds")])], sheet=[(1200, "m")])
    rows = [
        _placed("REG-p1-r01", "1200", "unit", "yds"),
        _placed("XLS-r2", "1200", "UOM", "m"),
    ]

    c4 = proof.check_c4(inputs, rows)

    assert c4.pass_
    assert set(c4.by_source_unit) == {"REGISTER|YDS", "SHEET|M"}
    assert c4.by_source_unit["REGISTER|YDS"].in_ == Decimal("1200")
    assert c4.by_source_unit["SHEET|M"].placed == Decimal("1200")
    assert c4.qty_unknown == []


def test_F07_C4_negative_included() -> None:
    inputs = _inputs(erp=[_erp_line("100", "PCS"), _erp_line("-40", "PCS")])
    both = [_placed("ERP-0001", "100", "uom", "PCS"), _placed("ERP-0002", "-40", "uom", "PCS")]

    c4 = proof.check_c4(inputs, both)
    assert c4.pass_
    assert c4.by_source_unit["ERP|PCS"].in_ == Decimal("60")

    assert not proof.check_c4(inputs, both[:1]).pass_  # the negative row is not dropped


def test_F07_C4_null_qty_listed_never_summed() -> None:
    page = _full_page([_merged_row(1, "10", "kg"), _merged_row(2, "5", "kg")])
    unclear = (
        page.rows[1]
        .cells[RegisterField.QTY]
        .model_copy(update={"merged": Cell(value=None, status=CellStatus.ILLEGIBLE)})
    )
    row2 = page.rows[1].model_copy(
        update={"cells": {**page.rows[1].cells, RegisterField.QTY: unclear}}
    )
    page = page.model_copy(update={"rows": [page.rows[0], row2]})
    rows = [
        _placed("REG-p1-r01", "10", "unit", "kg"),
        _placed("REG-p1-r02", None, "unit", "kg"),
    ]

    c4 = proof.check_c4(_inputs([page]), rows)

    assert c4.pass_
    assert c4.qty_unknown == ["REG-p1-r02"]
    assert c4.by_source_unit["REGISTER|KG"].in_ == Decimal("10")


def test_F07_C4_non_numeric_qty_is_unknown() -> None:
    """A qty that is not a finite number goes to `qty_unknown`, never summed (F-07.R4, R10)."""
    inputs = _inputs(erp=[_erp_line("12m", "PCS"), _erp_line("5", "PCS")], sheet=[("abc", "m")])
    rows = [
        _placed("ERP-0001", None, "uom", "PCS"),
        _placed("ERP-0002", "5", "uom", "PCS"),
        _placed("XLS-r2", None, "UOM", "m"),
    ]

    c4 = proof.check_c4(inputs, rows)

    assert c4.pass_
    assert c4.qty_unknown == ["ERP-0001", "XLS-r2"]
    assert c4.by_source_unit["ERP|PCS"].in_ == Decimal("5")
    assert "SHEET|M" not in c4.by_source_unit


@pytest.mark.parametrize("qty", ["1_000", "1000000000000000", "NaN", "Infinity"])
def test_F07_C4_qty_normalise_rejects_is_unknown(qty: str) -> None:
    """Proof rejects exactly what normalise rejects (underscore numerals, non-finite,
    adjusted() >= 15), so C4 lists the row instead of failing."""
    inputs = _inputs(erp=[_erp_line(qty, "PCS"), _erp_line("5", "PCS")])
    rows = [_placed("ERP-0001", None, "uom", "PCS"), _placed("ERP-0002", "5", "uom", "PCS")]

    c4 = proof.check_c4(inputs, rows)

    assert c4.pass_
    assert c4.qty_unknown == ["ERP-0001"]
    assert c4.by_source_unit["ERP|PCS"].in_ == Decimal("5")


def test_F07_C4_largest_accepted_qty_is_summed() -> None:
    inputs = _inputs(erp=[_erp_line("999999999999999", "PCS")])
    rows = [_placed("ERP-0001", "999999999999999", "uom", "PCS")]

    c4 = proof.check_c4(inputs, rows)

    assert c4.pass_
    assert c4.qty_unknown == []


@pytest.mark.parametrize("sep", ["\u2028", "\x0c", "\x1c", "\x85"])
def test_F07_C3_erp_line_count_splits_on_newline_only(sep: str) -> None:
    line = _erp_line("1", "PCS").replace("ITEM", f"IT{sep}EM")
    data = f"{ERP_HEADER}\r\n{line}\r\n".encode("utf-8-sig")
    assert proof.count_erp_lines(data) == 1
    assert proof.count_erp_lines(data + b"\n") == 1


def test_F07_C4_resolved_override_without_value_ignored() -> None:
    """A `RESOLVED` CELL item with no `resolved_value` does not replace the merged qty (F-04.R2)."""
    page = _full_page([_merged_row(1, "10", "kg")])
    valueless = make_review(RUN, "CELL:REG-p1-r01:qty", status=ReviewStatus.RESOLVED)

    c4 = proof.check_c4(
        _inputs([page], decisions=[valueless]), [_placed("REG-p1-r01", "10", "unit", "kg")]
    )

    assert c4.pass_
    assert c4.qty_unknown == []
    assert c4.by_source_unit["REGISTER|KG"].in_ == Decimal("10")


# --- C1, C2 ---------------------------------------------------------------------------------


def test_F07_C1_unplaced_row_fails() -> None:
    """C1 compares F-04's rows in with rows carrying an outcome, not the count with itself."""
    rows = [
        make_row(RUN, "ERP-0001", outcome=Outcome.MATCHED),
        make_row(RUN, "ERP-0002", outcome=None),
    ]

    c1 = proof.check_c1({Source.ERP: 2}, rows)

    assert not c1.pass_
    assert (c1.per_source[Source.ERP].in_, c1.per_source[Source.ERP].placed) == (2, 1)


def test_F07_C2_row_id_twice_in_rows_fails() -> None:
    rows = [
        make_row(RUN, "ERP-0001", outcome=Outcome.MATCHED),
        make_row(RUN, "ERP-0001", outcome=Outcome.MATCHED),
    ]

    c2 = proof.check_c2(rows, [])

    assert not c2.pass_
    assert c2.placed_twice == ["ERP-0001"]
    assert c2.unplaced == []


# --- C3 -------------------------------------------------------------------------------------


def _register_group(row_ids: list[str], outcome: Outcome) -> Group:
    return make_group(RUN, "GRN:1", grn_no=1, row_ids={Source.REGISTER: row_ids}, outcome=outcome)


def _row_in_one_read(row_index: int) -> ReviewItem:
    return make_review(
        RUN,
        f"CELL:REG-p1-r{row_index:02}:row",
        source_row_id=f"REG-p1-r{row_index:02}",
        field=None,
        reason=ReviewReason.ROW_IN_ONE_READ,
        read_a=None,
        read_b=None,
        context=RowInOneReadContext(page_no=1, row_index=row_index, read=ReadVariant.A, cells={}),
    )


def _one_read_page() -> RegisterPageInput:
    rows = [_merged_row(1, "1", "kg"), _merged_row(2, "1", "kg", in_b=False)]
    return _page(rows, count_a=2, count_b=1, blank_a=14, blank_b=15)


def test_F07_C3_slot_mismatch_fails() -> None:
    page = _page([_merged_row(1, "1", "kg")], count_a=1, count_b=1, blank_a=14, blank_b=14)
    inputs = _inputs([page])

    c3 = proof.check_c3(inputs, {Source.REGISTER: 1, Source.ERP: 0, Source.SHEET: 0}, [])

    assert not c3.pass_
    assert c3.problems == [
        "page 1 read A: 1 rows + 14 blank != 16",
        "page 1 read B: 1 rows + 14 blank != 16",
        "page 1: 1 merged rows + 14 blank != 16",
    ]


def test_F07_C3_reads_disagree_fails() -> None:
    inputs = _inputs([_one_read_page()])
    groups = [_register_group(["REG-p1-r01", "REG-p1-r02"], Outcome.NEEDS_REVIEW)]

    c3 = proof.check_c3(inputs, {Source.REGISTER: 2, Source.ERP: 0, Source.SHEET: 0}, groups)

    assert not c3.pass_
    assert c3.problems == ["REG-p1-r02: in one read only, no ROW_IN_ONE_READ item"]


def test_F07_C3_reads_disagree_no_one_read_row_fails() -> None:
    """Reads disagree on row count, but both rows appear in both reads (no one-read row) (R9)."""
    rows = [_merged_row(1, "1", "kg"), _merged_row(2, "1", "kg")]
    page = _page(rows, count_a=2, count_b=1, blank_a=14, blank_b=15)
    inputs = _inputs([page])

    c3 = proof.check_c3(inputs, {Source.REGISTER: 2, Source.ERP: 0, Source.SHEET: 0}, [])

    assert not c3.pass_
    assert c3.problems == ["page 1: reads disagree on row count, no one-read row found"]


def test_F07_C3_row_in_one_read_covered_passes() -> None:
    inputs = _inputs([_one_read_page()], decisions=[_row_in_one_read(2)])
    rows_in = {Source.REGISTER: 2, Source.ERP: 0, Source.SHEET: 0}
    review = [_register_group(["REG-p1-r01", "REG-p1-r02"], Outcome.NEEDS_REVIEW)]
    matched = [_register_group(["REG-p1-r01", "REG-p1-r02"], Outcome.MATCHED)]

    c3 = proof.check_c3(inputs, rows_in, review)
    assert c3.pass_
    assert c3.per_source[Source.REGISTER] == 2

    assert not proof.check_c3(inputs, rows_in, matched).pass_  # open item: must stay NEEDS_REVIEW


# --- C5 -------------------------------------------------------------------------------------


def _receipts_and_groups(*grns: int) -> tuple[list[CanonicalRow], list[Group]]:
    rows = [
        make_row(RUN, f"ERP-{n:04}", grn_no=n, row_kind=RowKind.RECEIPT, outcome=Outcome.MISSING)
        for n in grns
    ]
    groups = [
        make_group(RUN, f"GRN:{n}", grn_no=n, outcome=Outcome.MISSING, missing_from=[Source.SHEET])
        for n in grns
    ]
    return rows, groups


def _gap(low: int, high: int) -> ControlFlag:
    detail = {"low": str(low), "high": str(high)}
    return make_flag(RUN, flag=FlagType.SEQUENCE_GAP, grn_no=low, detail=detail)


def test_F07_C5_unflagged_gap_fails() -> None:
    rows, groups = _receipts_and_groups(1, 3)

    c5 = proof.check_c5(rows, groups, [])
    assert not c5.pass_
    assert (c5.grns, c5.gaps) == (2, [2])

    assert proof.check_c5(rows, groups, [_gap(2, 2)]).pass_


def test_F07_C5_range_flag_covers_every_missing_grn() -> None:
    rows, groups = _receipts_and_groups(1, 5)

    c5 = proof.check_c5(rows, groups, [_gap(2, 4)])
    assert c5.pass_
    assert c5.gaps == [2, 3, 4]

    assert not proof.check_c5(rows, groups, [_gap(2, 3)]).pass_  # 4 is not covered
    assert proof.check_c5(rows, groups, [_gap(2, 2), _gap(3, 4)]).pass_


def test_F07_C5_non_receipt_grn_is_not_a_gap() -> None:
    """C5 uses the SEQUENCE_GAP rule's GRN set: all rows, any kind (user decision 2026-10-07)."""
    rows, groups = _receipts_and_groups(1, 3)
    rows.append(make_row(RUN, "XLS-r2", grn_no=2, row_kind=RowKind.NON_RECEIPT))

    c5 = proof.check_c5(rows, groups, [])
    assert c5.pass_
    assert c5.gaps == []


# --- Independence (F-07.R3) -----------------------------------------------------------------


def test_F07_independence() -> None:
    """proof.py never imports from `app.pipeline.normalise` (or its symbols) nor uses the main
    parsers by name (F-07.R3)."""
    source = Path(proof.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    import_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_symbols = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    used |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}

    assert "app.pipeline.normalise" not in modules
    assert "app.pipeline.normalise" not in import_names

    forbidden = {"read_erp", "read_sheet", "normalise_all"}
    assert not forbidden & imported_symbols
    assert not forbidden & import_names
    assert not forbidden & used
