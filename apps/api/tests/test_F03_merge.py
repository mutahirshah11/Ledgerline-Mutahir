"""F-03.R7, R8: cell and row merge (data-model §3 precedence table, FR-015 to FR-020)."""

import json
from pathlib import Path
from typing import Any

import pytest

from app.domain.enums import CellStatus, RegisterField, ReviewReason
from app.domain.models import Cell, PageRead, RowRead
from app.pipeline.extract import merge_cells, merge_reads, parse_read

GT_DIR = Path(__file__).resolve().parents[1] / "tests" / "ground_truth"
SHA = "a" * 64

CLEAR, UNCLEAR = CellStatus.CLEAR, CellStatus.UNCLEAR
CORRECTED, ILLEGIBLE = CellStatus.CORRECTED, CellStatus.ILLEGIBLE


def clear(value: str | None, note: str | None = None) -> Cell:
    return Cell(value=value, status=CLEAR, note=note)


def unclear(*candidates: str, note: str | None = None) -> Cell:
    return Cell(value=None, status=UNCLEAR, candidates=list(candidates), note=note)


def corrected(value: str, struck: str) -> Cell:
    return Cell(value=value, status=CORRECTED, struck=struck)


def illegible(note: str | None = None) -> Cell:
    return Cell(value=None, status=ILLEGIBLE, note=note)


def row(index: int, **cells: Cell) -> RowRead:
    return RowRead(
        row_index=index,
        cells={f: cells.get(f.value, clear(f"{f.value}{index}")) for f in RegisterField},
    )


def page(*rows: RowRead, **over: Any) -> PageRead:
    base: dict[str, Any] = dict(
        month_written="Sep 2026",
        page_no_written="2",
        table_top=0.1,
        table_bottom=0.9,
        blank_row_slots=16 - len(rows),
        rows=list(rows),
    )
    return PageRead(**(base | over))


def gt_read(page_no: int) -> PageRead:
    data = json.loads((GT_DIR / f"register_p{page_no}.json").read_text(encoding="utf-8"))
    del data["page_no"], data["source"]
    return parse_read(data)


# --- merge_cells -------------------------------------------------------------------------


def test_F03_R7_clear_same() -> None:
    m = merge_cells(clear("Qasr Pkg"), clear("qasr pkg "))
    assert m.merged == clear("Qasr Pkg")
    assert m.review_reason is None
    m = merge_cells(clear("  2000 "), clear("2000"))
    assert m.merged.value == "2000"
    blank = merge_cells(clear(None), clear(None))
    assert blank.merged == clear(None) and blank.review_reason is None


def test_F03_R7_clear_different_reads_disagree() -> None:
    m = merge_cells(clear("2000"), clear("2800"))
    assert m.merged.status is UNCLEAR
    assert m.merged.value is None
    assert m.merged.candidates == ["2000", "2800"]
    assert m.review_reason is ReviewReason.READS_DISAGREE


def test_F03_R7_blank_vs_value() -> None:
    for a, b, blank_read in ((clear(None), clear("x"), "A"), (clear("x"), clear(None), "B")):
        m = merge_cells(a, b)
        assert m.merged.status is UNCLEAR
        assert m.merged.value is None
        assert m.merged.candidates == ["x"]
        assert m.review_reason is ReviewReason.READS_DISAGREE
        assert m.merged.note is not None
        assert f"read {blank_read} saw the cell blank" in m.merged.note


@pytest.mark.parametrize(
    "other", [clear("5"), unclear("5", "6"), corrected("66", "60"), illegible()]
)
def test_F03_R7_illegible_wins(other: Cell) -> None:
    for a, b in ((illegible("ink blot"), other), (other, illegible("ink blot"))):
        m = merge_cells(a, b)
        assert m.merged.status is ILLEGIBLE
        assert m.merged.value is None
        assert m.merged.candidates == []
        assert m.merged.struck is None
        assert m.merged.note == "ink blot"
        assert m.review_reason is ReviewReason.ILLEGIBLE


def test_F03_R7_corrected_same() -> None:
    m = merge_cells(corrected("66", "60"), corrected(" 66", "60 "))
    assert m.merged.status is CORRECTED
    assert m.merged.value == "66"
    assert m.merged.struck == "60"
    assert m.merged.candidates == []
    assert m.review_reason is ReviewReason.CORRECTED


@pytest.mark.parametrize(
    "b", [corrected("68", "60"), corrected("66", "50")], ids=["current", "struck"]
)
def test_F03_R7_corrected_differs(b: Cell) -> None:
    m = merge_cells(corrected("66", "60"), b)
    assert m.merged.status is UNCLEAR
    assert m.merged.value is None
    assert m.merged.struck is None
    assert m.merged.candidates == (["66", "68"] if b.value == "68" else ["66"])
    assert m.review_reason is ReviewReason.READS_DISAGREE


@pytest.mark.parametrize(
    ("other", "candidates"),
    [(clear("66"), ["66"]), (clear("60"), ["66", "60"]), (unclear("68", "66"), ["66", "68"])],
)
def test_F03_R7_corrected_vs_clear_and_unclear(other: Cell, candidates: list[str]) -> None:
    for a, b, expected in (
        (corrected("66", "60"), other, candidates),
        (other, corrected("66", "60"), None),
    ):
        m = merge_cells(a, b)
        assert m.merged.status is UNCLEAR
        assert m.merged.value is None
        assert m.review_reason is ReviewReason.READS_DISAGREE  # Clarification 3
        if expected is not None:
            assert m.merged.candidates == expected


def test_F03_R7_unclear_union() -> None:
    m = merge_cells(unclear("4419", "4479"), unclear(" 4479", "4419 ", "4449"))
    assert m.merged.candidates == ["4419", "4479", "4449"]
    assert m.review_reason is ReviewReason.UNCLEAR
    m = merge_cells(clear("Qasr Pkg"), unclear("qasr pkg", "Qasim Pkg"))
    assert m.merged.candidates == ["Qasr Pkg", "Qasim Pkg"]
    assert m.merged.status is UNCLEAR and m.merged.value is None
    assert m.review_reason is ReviewReason.UNCLEAR


def test_F03_R7_empty_union_becomes_illegible() -> None:
    m = merge_cells(unclear("  "), clear(None))
    assert m.merged == Cell(value=None, status=ILLEGIBLE)
    assert m.review_reason is ReviewReason.UNCLEAR  # same reason as the row matched


def test_F03_R7_originals_kept() -> None:
    a, b = clear("2000"), unclear("2800", "2000")
    m = merge_cells(a, b)
    assert m.read_a == a
    assert m.read_b == b


def test_F03_R7_note_join() -> None:
    assert merge_cells(clear("1", "faint"), clear("1", "smudge")).merged.note == "faint | smudge"
    assert merge_cells(clear("1", "faint"), clear("1", " Faint")).merged.note == "faint"
    assert merge_cells(clear("1"), clear("1", "smudge")).merged.note == "smudge"
    assert merge_cells(clear("1", "faint"), clear("1")).merged.note == "faint"
    assert merge_cells(clear("1"), clear("1")).merged.note is None


# --- merge_reads -------------------------------------------------------------------------


def test_F03_R8_row_in_one_read() -> None:
    single = row(15, qty=corrected("66", "60"), lot_no=clear(None), unit=unclear("kg", "pcs"))
    for read_a, read_b in (
        (page(row(1), single), page(row(1))),
        (page(row(1)), page(row(1), single)),
    ):
        merged = merge_reads(read_a, read_b, page_no=2, page_sha256=SHA)
        assert [r.row_index for r in merged.rows] == [1, 15]
        kept = merged.rows[1]
        for field, cell in kept.cells.items():
            assert cell.review_reason is None
            assert cell.merged.status in (UNCLEAR, ILLEGIBLE)
            assert cell.merged.value is None
            alone = cell.read_a if cell.read_b is None else cell.read_b
            assert alone == single.cells[field]
            assert (cell.read_a is None) != (cell.read_b is None)
        assert kept.cells[RegisterField.QTY].merged.candidates == ["66"]
        assert kept.cells[RegisterField.DATE].merged.candidates == ["date15"]
        assert kept.cells[RegisterField.LOT_NO].merged.status is ILLEGIBLE
        assert kept.cells[RegisterField.UNIT].merged.status is ILLEGIBLE


def test_F03_R8_rows_sorted_fixed_field_order() -> None:
    read_a = page(row(9), row(2), row(5))
    read_b = page(row(5), row(9), row(2), row(3))
    merged = merge_reads(read_a, read_b, page_no=2, page_sha256=SHA)
    assert [r.row_index for r in merged.rows] == [2, 3, 5, 9]
    for r in merged.rows:
        assert r.page_no == 2
        assert list(r.cells) == list(RegisterField)
    assert merged.row_count_a == 3 and merged.row_count_b == 4
    assert merged.page_no == 2 and merged.page_sha256 == SHA


def test_F03_R7_deterministic() -> None:
    read_a = page(row(3, qty=unclear("5", "6")), row(1), row(7, grn_no=clear("4401")))
    read_b = page(row(1, supplier=clear("other")), row(3, qty=unclear("6", "8")), row(12))
    first = merge_reads(read_a, read_b, page_no=1, page_sha256=SHA).model_dump_json()
    second = merge_reads(read_a, read_b, page_no=1, page_sha256=SHA).model_dump_json()
    assert first == second


@pytest.mark.parametrize("page_no", [1, 2])
def test_F03_R7_ground_truth_paired_with_itself_is_unchanged(page_no: int) -> None:
    gt = gt_read(page_no)
    merged = merge_reads(gt, gt, page_no=page_no, page_sha256=SHA)
    assert [r.row_index for r in merged.rows] == [r.row_index for r in gt.rows]
    for gt_row, m_row in zip(gt.rows, merged.rows, strict=True):
        for field in RegisterField:
            want, got = gt_row.cells[field], m_row.cells[field]
            assert got.merged.status is want.status
            assert got.merged.value == (want.value.strip() if want.value else want.value)
            assert got.merged.struck == want.struck
            assert got.read_a == want and got.read_b == want
    by_grn = {
        r.cells[RegisterField.GRN_NO].merged.value: r.cells[RegisterField.QTY] for r in merged.rows
    }
    if page_no == 2:
        assert by_grn["4419"].merged.status is CORRECTED
        assert by_grn["4419"].merged.value == "66"
        assert by_grn["4419"].merged.struck == "60"
        assert by_grn["4419"].review_reason is ReviewReason.CORRECTED
        assert by_grn["4428"].merged.status is ILLEGIBLE
        assert by_grn["4428"].merged.value is None
        assert by_grn["4428"].review_reason is ReviewReason.ILLEGIBLE
    else:
        assert all(c.review_reason is None for r in merged.rows for c in r.cells.values())
