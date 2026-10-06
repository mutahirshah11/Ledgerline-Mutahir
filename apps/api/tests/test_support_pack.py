"""Test support (plan.md, research R4/R15): `build_engine_inputs` turns the pack and the ground
truth into the engine's inputs, with read A = read B = ground truth."""

from pathlib import Path
from uuid import UUID

import pytest

from app.domain.enums import (
    CellStatus,
    RegisterField,
    ReviewKind,
    ReviewReason,
    ReviewStatus,
    RunStatus,
)
from app.domain.models import Run
from app.pipeline.engine import EngineInputs
from app.pipeline.normalise import load_mappings
from tests.support.pack import build_engine_inputs, resolved_cell

RUN_ID = UUID("00000000-0000-4000-8000-000000000001")


@pytest.fixture(scope="module")
def built(pack_dir: Path, ground_truth_dir: Path) -> EngineInputs:
    return build_engine_inputs(pack_dir, ground_truth_dir, RUN_ID)


def test_two_pages_of_fourteen_rows(built: EngineInputs) -> None:
    assert [p.page_no for p in built.register_pages] == [1, 2]
    for page in built.register_pages:
        assert (page.row_count_a, page.row_count_b) == (14, 14)
        assert (page.blank_slots_a, page.blank_slots_b) == (2, 2)
        assert page.month_written == "Sep 2026"
        assert [r.row_index for r in page.rows] == list(range(1, 15))


def test_merged_cells_are_both_reads_of_ground_truth(built: EngineInputs) -> None:
    qty = built.register_pages[1].rows[13].cells[RegisterField.QTY]
    assert qty.merged.status is CellStatus.ILLEGIBLE
    assert qty.read_a == qty.read_b == qty.merged
    assert qty.review_reason is ReviewReason.ILLEGIBLE
    clear = built.register_pages[0].rows[0].cells[RegisterField.SUPPLIER]
    assert (clear.merged.value, clear.review_reason) == ("Qasr Pkg", None)


def test_pack_bytes_mappings_and_month(built: EngineInputs, pack_dir: Path) -> None:
    assert built.run_id == RUN_ID
    assert built.month == "2026-09"
    assert built.erp_bytes == (pack_dir / "t4_erp_grn.csv").read_bytes()
    assert built.sheet_bytes == (pack_dir / "t4_store_sheet.xlsx").read_bytes()
    assert built.mappings == load_mappings()
    assert built.decisions == []


def test_decisions_are_passed_through(pack_dir: Path, ground_truth_dir: Path) -> None:
    decision = resolved_cell(RUN_ID, "REG-p2-r14", "qty", "70")
    built = build_engine_inputs(pack_dir, ground_truth_dir, RUN_ID, decisions=[decision])
    assert built.decisions == [decision]


def test_resolved_cell_is_a_resolved_cell_item() -> None:
    item = resolved_cell(RUN_ID, "REG-p2-r14", "qty", "70")
    assert (item.kind, item.status, item.resolved_value) == (
        ReviewKind.CELL,
        ReviewStatus.RESOLVED,
        "70",
    )
    assert (item.item_key, item.source_row_id, item.field) == (
        "CELL:REG-p2-r14:qty",
        "REG-p2-r14",
        "qty",
    )
    assert item == resolved_cell(RUN_ID, "REG-p2-r14", "qty", "70")
    assert item.reason is ReviewReason.ILLEGIBLE


def test_resolved_cell_reason_keyword() -> None:
    item = resolved_cell(RUN_ID, "REG-p2-r05", "qty", "66", reason=ReviewReason.CORRECTED)
    assert item.reason is ReviewReason.CORRECTED


def test_pack_run_id_is_shared() -> None:
    """One fixed pack run id, defined in tests/support/pack.py and reused everywhere."""
    from tests import conftest
    from tests.support import normalised, pack

    assert normalised.PACK_RUN_ID is pack.PACK_RUN_ID
    assert conftest.PACK_RUN_ID is pack.PACK_RUN_ID


def test_extracted_run_shares_engine_inputs_run_id(
    extracted_run: Run, engine_inputs: EngineInputs
) -> None:
    assert extracted_run.status is RunStatus.EXTRACTED
    assert engine_inputs.run_id == extracted_run.id
