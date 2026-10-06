"""Builds `EngineInputs` from the pack and the ground truth so every test builds inputs the same
way (plan.md; research R4, R15). Read A = read B = ground truth, merged per F-03.R7."""

from collections.abc import Iterable
from pathlib import Path
from uuid import UUID

from app.domain.enums import CellStatus, ReviewKind, ReviewReason, ReviewStatus
from app.domain.ids import register_row_id, review_item_key, stable_id
from app.domain.models import (
    Cell,
    CellContext,
    GroundTruthPage,
    MergedCell,
    MergedRow,
    RegisterPageInput,
    ReviewItem,
)
from app.pipeline.engine import EngineInputs
from app.pipeline.normalise import load_mappings
from tests.factories import T0

PACK_RUN_ID = UUID("00000000-0000-4000-8000-000000002609")
"""Fixed run id of the pack run, so stable ids (F-05.R11) are the same in every test."""
PACK_MONTH = "2026-09"
PAGES = (1, 2)
ERP_FILE = "t4_erp_grn.csv"
SHEET_FILE = "t4_store_sheet.xlsx"


def _merged(cell: Cell) -> MergedCell:
    """Two identical reads (F-03.R7): CLEAR needs no review; otherwise the reason is the status."""
    reason = None if cell.status is CellStatus.CLEAR else ReviewReason(cell.status.value)
    return MergedCell(merged=cell, read_a=cell, read_b=cell, review_reason=reason)


def _page_input(page: GroundTruthPage) -> RegisterPageInput:
    rows = [
        MergedRow(
            page_no=page.page_no,
            row_index=r.row_index,
            cells={f: _merged(c) for f, c in r.cells.items()},
        )
        for r in page.rows
    ]
    return RegisterPageInput(
        page_no=page.page_no,
        month_written=page.month_written,
        row_count_a=len(rows),
        row_count_b=len(rows),
        blank_slots_a=page.blank_row_slots,
        blank_slots_b=page.blank_row_slots,
        rows=rows,
    )


def build_engine_inputs(
    pack_dir: Path, ground_truth_dir: Path, run_id: UUID, decisions: Iterable[ReviewItem] = ()
) -> EngineInputs:
    """Engine inputs for the Kestrel 2026-09 pack: register pages from the ground truth, the ERP
    and sheet bytes, the shipped mappings and the given decisions."""
    pages = [
        _page_input(
            GroundTruthPage.model_validate_json(
                (ground_truth_dir / f"register_p{p}.json").read_bytes()
            )
        )
        for p in PAGES
    ]
    return pack_engine_inputs(pack_dir, run_id, pages, decisions)


def pack_engine_inputs(
    pack_dir: Path,
    run_id: UUID,
    register_pages: list[RegisterPageInput],
    decisions: Iterable[ReviewItem] = (),
) -> EngineInputs:
    """Engine inputs for the pack with the given register pages: the ERP and sheet bytes, the
    shipped mappings and the given decisions."""
    return EngineInputs(
        run_id=run_id,
        month=PACK_MONTH,
        register_pages=register_pages,
        erp_bytes=(pack_dir / ERP_FILE).read_bytes(),
        sheet_bytes=(pack_dir / SHEET_FILE).read_bytes(),
        mappings=load_mappings(),
        decisions=list(decisions),
    )


def resolved_cell(
    run_id: UUID,
    row_id: str,
    field: str,
    value: str,
    *,
    reason: ReviewReason = ReviewReason.ILLEGIBLE,
) -> ReviewItem:
    """A `RESOLVED` CELL decision (F-04.R2) on register row `row_id` (`REG-p{p}-r{rr}`) and
    register field `field`, raised for `reason`, with deterministic id and timestamp."""
    page_text, row_text = row_id.removeprefix("REG-p").split("-r")
    page_no, row_index = int(page_text), int(row_text)
    if register_row_id(page_no, row_index) != row_id:
        raise ValueError(f"not a register row id: {row_id!r}")
    key = review_item_key(ReviewKind.CELL, row_id=row_id, field=field)
    return ReviewItem(
        id=stable_id(run_id, key),
        run_id=run_id,
        kind=ReviewKind.CELL,
        item_key=key,
        grn_no=None,
        source_row_id=row_id,
        field=field,
        reason=reason,
        read_a=None,
        read_b=None,
        context=CellContext(page_no=page_no, row_index=row_index),
        status=ReviewStatus.RESOLVED,
        resolved_value=value,
        resolved_by="reviewer@example.com",
        resolved_at=T0,
    )
