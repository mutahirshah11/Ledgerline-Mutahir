"""F-03 Outputs, Section 20: CELL review items from a merged page (data-model §4,
FR-019, FR-022, FR-024 to FR-026)."""

from datetime import datetime
from uuid import UUID

from app.core.clock import KARACHI
from app.core.memory_repo import MemoryRepo
from app.domain.enums import (
    CellStatus,
    ReadVariant,
    RegisterField,
    ReviewKind,
    ReviewReason,
    ReviewStatus,
)
from app.domain.ids import stable_id
from app.domain.models import (
    Cell,
    CellContext,
    PageLabelMismatchContext,
    PageRead,
    ReviewItem,
    RowInOneReadContext,
    RowRead,
)
from app.pipeline.extract import build_review_items, merge_reads
from tests.factories import make_run

SHA = "c" * 64
RUN_ID = UUID("00000000-0000-4000-8000-000000000013")
MONTH = "2026-09"


def clear(value: str | None) -> Cell:
    return Cell(value=value, status=CellStatus.CLEAR)


def unclear(*candidates: str) -> Cell:
    return Cell(value=None, status=CellStatus.UNCLEAR, candidates=list(candidates))


def row(index: int, **cells: Cell) -> RowRead:
    defaults = {RegisterField.GRN_NO: clear(f"44{index:02}")}
    return RowRead(
        row_index=index,
        cells={
            f: cells.get(f.value, defaults.get(f, clear(f"{f.value}{index}")))
            for f in RegisterField
        },
    )


def page(*rows: RowRead, **over: object) -> PageRead:
    base: dict[str, object] = dict(
        month_written="Sep 2026",
        page_no_written="2",
        table_top=0.1,
        table_bottom=0.9,
        blank_row_slots=16 - len(rows),
        rows=list(rows),
    )
    return PageRead.model_validate(base | over)


def items_for(a: PageRead, b: PageRead, page_no: int = 2) -> list[ReviewItem]:
    merged = merge_reads(a, b, page_no=page_no, page_sha256=SHA)
    return build_review_items(merged, a, b, run_id=RUN_ID, run_month=MONTH)


def test_F03_R7_cell_item_key_and_fields() -> None:
    a_qty = Cell(value="2000", status=CellStatus.CLEAR, note="faint")
    b_qty = Cell(value="2800", status=CellStatus.CLEAR)
    a, b = page(row(14, qty=a_qty)), page(row(14, qty=b_qty))
    [item] = items_for(a, b)
    assert item.item_key == "CELL:REG-p2-r14:qty"
    assert item.id == stable_id(RUN_ID, item.item_key)
    assert item.run_id == RUN_ID
    assert item.kind is ReviewKind.CELL
    assert item.status is ReviewStatus.OPEN
    assert item.reason is ReviewReason.READS_DISAGREE
    assert item.source_row_id == "REG-p2-r14"
    assert item.field == "qty"
    assert item.grn_no == 4414
    assert item.read_a == a_qty and item.read_b == b_qty
    assert item.context == CellContext(
        page_no=2, row_index=14, candidates=["2000", "2800"], struck=None, note="faint"
    )
    corrected = Cell(value="66", status=CellStatus.CORRECTED, struck="60")
    [item] = items_for(page(row(5, qty=corrected)), page(row(5, qty=corrected)))
    assert item.reason is ReviewReason.CORRECTED
    assert isinstance(item.context, CellContext)
    assert item.context.struck == "60"


def test_F03_R7_only_reviewable_fields_queued() -> None:
    a = page(row(3, item=clear("Cartons"), recd_by=unclear("B.H.", "R.H.")))
    b = page(row(3, item=clear("Cartns"), recd_by=clear("B.H.")))
    assert items_for(a, b) == []
    a = page(row(3, item=clear("Cartons"), unit=unclear("kg", "pcs")))
    b = page(row(3, item=clear("Cartns"), unit=clear("kg")))
    assert [i.field for i in items_for(a, b)] == ["unit"]


def test_F03_R7_grn_filled_only_when_clear_numeric() -> None:
    qty_a, qty_b = clear("10"), clear("12")
    cases: list[tuple[Cell, Cell, int | None]] = [
        (clear("4407"), clear("4407"), 4407),
        (clear(" 4407 "), clear("4407"), 4407),
        (clear("4407"), clear("4401"), None),  # grn itself disagrees
        (clear("44O7"), clear("44o7"), None),  # not all digits
        (clear(None), clear(None), None),
        (unclear("4407", "4401"), unclear("4407"), None),
    ]
    for grn_a, grn_b, expected in cases:
        a = page(row(7, grn_no=grn_a, qty=qty_a))
        b = page(row(7, grn_no=grn_b, qty=qty_b))
        items = items_for(a, b)
        qty_item = next(i for i in items if i.field == "qty")
        assert qty_item.grn_no == expected, (grn_a, grn_b)
        assert all(i.grn_no == expected for i in items)


def test_F03_R8_row_in_one_read_single_item() -> None:
    lonely = row(15, qty=unclear("5", "6"))
    for a, b, variant in (
        (
            page(row(1), lonely, page_no_written="1"),
            page(row(1), page_no_written="1"),
            ReadVariant.A,
        ),
        (
            page(row(1), page_no_written="1"),
            page(row(1), lonely, page_no_written="1"),
            ReadVariant.B,
        ),
    ):
        [item] = items_for(a, b, page_no=1)
        assert item.item_key == "CELL:REG-p1-r15:row"
        assert item.kind is ReviewKind.CELL
        assert item.reason is ReviewReason.ROW_IN_ONE_READ
        assert item.source_row_id == "REG-p1-r15"
        assert item.field == "row"
        assert item.read_a is None and item.read_b is None
        assert item.grn_no is None  # merged grn_no is UNCLEAR for a one-read row
        assert item.context == RowInOneReadContext(
            page_no=1, row_index=15, read=variant, cells=lonely.cells
        )


def test_F03_R9_page_item() -> None:
    a = page(row(1), page_no_written="3")
    b = page(row(1), month_written="Aug 2026")
    [item] = items_for(a, b)
    assert item.item_key == "CELL:REG-p2:page"
    assert item.kind is ReviewKind.CELL
    assert item.reason is ReviewReason.PAGE_LABEL_MISMATCH
    assert item.source_row_id is None
    assert item.field == "page"
    assert item.grn_no is None
    assert item.read_a is None and item.read_b is None
    assert item.context == PageLabelMismatchContext(
        page_no=2,
        run_month=MONTH,
        month_written={ReadVariant.A: "Sep 2026", ReadVariant.B: "Aug 2026"},
        page_no_written={ReadVariant.A: "3", ReadVariant.B: "2"},
    )


def test_F03_R7_upsert_keeps_decision() -> None:
    repo = MemoryRepo()
    repo.create_run(make_run(id=RUN_ID))
    a, b = page(row(4, qty=clear("10"))), page(row(4, qty=clear("12")))
    [item] = repo.upsert_review_items(RUN_ID, items_for(a, b))
    at = datetime(2026, 10, 15, 9, 0, tzinfo=KARACHI)
    repo.resolve_review_item(
        item.id,
        status=ReviewStatus.RESOLVED,
        resolved_value="12",
        resolved_by="controller@example.com",
        resolved_at=at,
        note=None,
    )
    again = items_for(a, b)
    assert [i.id for i in again] == [item.id]  # same key, same stable id
    repo.upsert_review_items(RUN_ID, again)
    [stored] = repo.list_review_items(RUN_ID)
    assert stored.status is ReviewStatus.RESOLVED
    assert stored.resolved_value == "12"
    assert stored.resolved_at == at


def test_F03_R7_items_deterministic_order() -> None:
    a = page(
        row(9, qty=clear("1")),
        row(2, date=clear("01/09"), unit=clear("kg")),
        row(12),
        page_no_written="9",
    )
    b = page(row(2, date=clear("02/09"), unit=clear("pcs")), row(9, qty=clear("2")), row(5))
    keys = [i.item_key for i in items_for(a, b)]
    assert keys == [
        "CELL:REG-p2:page",
        "CELL:REG-p2-r02:date",
        "CELL:REG-p2-r02:unit",
        "CELL:REG-p2-r05:row",
        "CELL:REG-p2-r09:qty",
        "CELL:REG-p2-r12:row",
    ]
    first = [i.model_dump_json() for i in items_for(a, b)]
    assert first == [i.model_dump_json() for i in items_for(a, b)]
