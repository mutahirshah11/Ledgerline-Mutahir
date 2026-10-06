"""F-03.R6, R9, FM-06: page-level values, label check and the unreadable-page rule
(FR-021 to FR-023)."""

from uuid import UUID

import pytest

from app.core.errors import PageUnreadable
from app.domain.enums import CellStatus, RegisterField, ReviewReason
from app.domain.models import Cell, PageRead, RowRead
from app.pipeline.extract import build_review_items, check_readable, merge_reads, page_label_ok

SHA = "b" * 64
RUN_ID = UUID("00000000-0000-4000-8000-000000000003")


def _row(index: int, illegible: int = 0) -> RowRead:
    """A row whose first `illegible` fields are ILLEGIBLE, the rest CLEAR."""
    fields = list(RegisterField)
    return RowRead(
        row_index=index,
        cells={
            f: Cell(value=None, status=CellStatus.ILLEGIBLE)
            if i < illegible
            else Cell(value=f"{f.value}{index}", status=CellStatus.CLEAR)
            for i, f in enumerate(fields)
        },
    )


def _read(rows: list[RowRead] | None = None, **over: object) -> PageRead:
    rows = [_row(1), _row(2)] if rows is None else rows
    base: dict[str, object] = dict(
        month_written="Sep 2026",
        page_no_written="2",
        table_top=0.15,
        table_bottom=0.95,
        blank_row_slots=16 - len(rows),
        rows=rows,
    )
    return PageRead.model_validate(base | over)


def _label_items(a: PageRead, b: PageRead) -> list[ReviewReason]:
    page = merge_reads(a, b, page_no=2, page_sha256=SHA)
    items = build_review_items(page, a, b, run_id=RUN_ID, run_month="2026-09")
    return [i.reason for i in items if i.reason is ReviewReason.PAGE_LABEL_MISMATCH]


# --- R6 page values ----------------------------------------------------------------------


def test_F03_R6_table_edges_mean_rounded() -> None:
    a = _read(table_top=0.1204, table_bottom=0.9)
    b = _read(table_top=0.1206, table_bottom=0.95)
    page = merge_reads(a, b, page_no=2, page_sha256=SHA)
    assert page.table_top == 0.121  # half-up, not banker's
    assert page.table_bottom == 0.925


def test_F03_R6_table_edges_one_or_none() -> None:
    a = _read(table_top=0.1234, table_bottom=None)
    b = _read(table_top=None, table_bottom=None)
    page = merge_reads(a, b, page_no=2, page_sha256=SHA)
    assert page.table_top == 0.123
    assert page.table_bottom is None
    page = merge_reads(b, _read(table_top=None, table_bottom=0.9456), page_no=2, page_sha256=SHA)
    assert page.table_top is None
    assert page.table_bottom == 0.946


def test_F03_R6_blank_slots_and_row_counts_per_read_kept() -> None:
    a = _read([_row(1), _row(2), _row(3)], blank_row_slots=13)
    b = _read([_row(1), _row(2)], blank_row_slots=12)
    page = merge_reads(a, b, page_no=2, page_sha256=SHA)
    assert (page.blank_row_slots_a, page.blank_row_slots_b) == (13, 12)
    assert (page.row_count_a, page.row_count_b) == (3, 2)
    assert page.month_written == "Sep 2026"
    assert page.page_no_written == "2"
    assert [r.row_index for r in page.rows] == [1, 2, 3]


# --- R9 label check ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "month",
    [
        "Sep 2026",
        "Sept 2026",
        "September 2026",
        "09/2026",
        "9/2026",
        "2026-09",
        "SEP 2026",
        "sept 2026",
        "september 2026",
        " Sep  2026 ",
    ],
)
def test_F03_R9_page_label_matches(month: str) -> None:
    assert page_label_ok(_read(month_written=month), page_no=2, run_month="2026-09")
    assert page_label_ok(_read(page_no_written=" 2 "), page_no=2, run_month="2026-09")


@pytest.mark.parametrize(
    "month", ["Aug 2026", "Sep 2025", "Oct 2026", "10/2026", "2026-9", "Sep", None, ""]
)
def test_F03_R9_month_not_named(month: str | None) -> None:
    assert not page_label_ok(_read(month_written=month), page_no=2, run_month="2026-09")


def test_F03_R9_page_no_mismatch_raises_item() -> None:
    for written in ("3", None, "two"):
        assert not page_label_ok(_read(page_no_written=written), page_no=2, run_month="2026-09")
    assert _label_items(_read(page_no_written="3"), _read()) == [ReviewReason.PAGE_LABEL_MISMATCH]


def test_F03_R9_month_mismatch_raises_item() -> None:
    assert _label_items(_read(), _read(month_written="Aug 2026")) == [
        ReviewReason.PAGE_LABEL_MISMATCH
    ]
    assert _label_items(_read(), _read()) == []


def test_F03_R9_reads_disagree_on_label_raises_item() -> None:
    a = _read(page_no_written="2", month_written="Sep 2026")
    b = _read(page_no_written="1", month_written="Sep 2026")
    assert _label_items(a, b) == [ReviewReason.PAGE_LABEL_MISMATCH]  # exactly one per page


# --- FM-06 unreadable page ---------------------------------------------------------------


def test_F03_FM06_more_than_half_illegible_rejected() -> None:
    # 2 rows x 8 cells = 16 cells; 9 illegible
    read = _read([_row(1, illegible=8), _row(2, illegible=1)])
    page = merge_reads(read, read, page_no=2, page_sha256=SHA)
    with pytest.raises(PageUnreadable) as caught:
        check_readable(page)
    assert caught.value.page_no == 2
    assert caught.value.illegible == 9
    assert caught.value.total == 16


def test_F03_FM06_exactly_half_not_rejected() -> None:
    read = _read([_row(1, illegible=4), _row(2, illegible=4)])
    page = merge_reads(read, read, page_no=2, page_sha256=SHA)
    check_readable(page)  # 8 of 16: not more than half
    empty = merge_reads(_read([]), _read([]), page_no=2, page_sha256=SHA)
    check_readable(empty)  # no rows: nothing to reject
