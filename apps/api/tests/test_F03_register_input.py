"""`register_input`: a merged page (E1) as the engine's `RegisterPageInput` (F-07.R3, blueprint
C23). Field names differ: `blank_row_slots_a/b` become `blank_slots_a/b`."""

from pathlib import Path

import pytest

from app.domain.enums import CellStatus, RegisterField
from app.domain.models import Cell, GroundTruthPage, PageRead, RowRead
from app.pipeline.extract import merge_reads, register_input
from tests.support.pack import _page_input

GT_DIR = Path(__file__).resolve().parent / "ground_truth"
SHA = "b" * 64


def _ground_truth(page_no: int) -> GroundTruthPage:
    return GroundTruthPage.model_validate_json(
        (GT_DIR / f"register_p{page_no}.json").read_text(encoding="utf-8")
    )


def _read(gt: GroundTruthPage) -> PageRead:
    return PageRead.model_validate(gt.model_dump(exclude={"page_no", "source"}))


def _row(index: int) -> RowRead:
    return RowRead(
        row_index=index,
        cells={f: Cell(value=f"{f.value}{index}", status=CellStatus.CLEAR) for f in RegisterField},
    )


def _page(*indexes: int, blank: int) -> PageRead:
    return PageRead(
        month_written="Sep 2026",
        page_no_written="2",
        table_top=0.1,
        table_bottom=0.9,
        blank_row_slots=blank,
        rows=[_row(i) for i in indexes],
    )


@pytest.mark.parametrize("page_no", [1, 2])
def test_F03_register_input_ground_truth_self_merge_matches_pack_input(page_no: int) -> None:
    gt = _ground_truth(page_no)
    merged = merge_reads(_read(gt), _read(gt), page_no=page_no, page_sha256=SHA)
    expected = _page_input(gt).model_copy(update={"page_sha256": SHA})
    assert register_input(merged) == expected


def test_F03_register_input_keeps_each_reads_counts_and_slots() -> None:
    """Rows seen by one read only: each read's row count and blank slots are carried (C23)."""
    merged = merge_reads(
        _page(1, 2, 3, blank=13), _page(1, 2, blank=14), page_no=2, page_sha256=SHA
    )
    got = register_input(merged)
    assert (got.row_count_a, got.row_count_b) == (3, 2)
    assert (got.blank_slots_a, got.blank_slots_b) == (13, 14)
    assert got.page_no == 2 and got.page_sha256 == SHA
    assert got.month_written == "Sep 2026"
    assert got.rows == merged.rows
    assert [r.row_index for r in got.rows] == [1, 2, 3]
