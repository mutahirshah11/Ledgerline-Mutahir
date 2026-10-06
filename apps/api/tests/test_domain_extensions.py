"""Phase 1 domain extensions (data-model.md Section 1: E1-E4)."""

from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.domain.enums import CellStatus, Fault, RowKind, Source
from app.domain.ids import stable_id
from app.domain.models import CanonicalRow, MergedCell, MergedRow, RegisterPageInput, RowMeta

from tests.factories import cell


def _merged_cell(value: str = "x") -> MergedCell:
    return MergedCell(merged=cell(value), read_a=cell(value), read_b=cell(value), review_reason=None)


def _merged_row(row_index: int) -> MergedRow:
    from app.domain.enums import RegisterField

    return MergedRow(
        page_no=1,
        row_index=row_index,
        cells={field: _merged_cell() for field in RegisterField},
    )


def _canonical_row(source: Source, cell_status: dict[str, CellStatus]) -> CanonicalRow:
    run_id = uuid4()
    return CanonicalRow(
        id=uuid4(),
        run_id=run_id,
        source=source,
        source_row_id="ERP-0001" if source is Source.ERP else "XLS-r2",
        row_kind=RowKind.RECEIPT,
        grn_no=1,
        date=None,
        supplier_code=None,
        item=None,
        lot_no=None,
        qty=None,
        uom=None,
        qty_base=None,
        base_uom=None,
        cell_status=cell_status,
        raw={},
        meta=RowMeta(),
        duplicate_of=None,
        group_id=None,
        outcome=None,
    )


_ALL_CLEAR = {
    "date": CellStatus.CLEAR,
    "grn_no": CellStatus.CLEAR,
    "supplier_code": CellStatus.CLEAR,
    "item": CellStatus.CLEAR,
    "lot_no": CellStatus.CLEAR,
    "qty": CellStatus.CLEAR,
    "uom": CellStatus.CLEAR,
    "received_by": CellStatus.CLEAR,
}


# --- E1: RegisterPageInput ------------------------------------------------------------------


def test_register_page_input_sorts_rows_by_row_index() -> None:
    page = RegisterPageInput(
        page_no=1,
        page_sha256=None,
        month_written="Sep 2026",
        row_count_a=2,
        row_count_b=2,
        blank_slots_a=14,
        blank_slots_b=14,
        rows=[_merged_row(2), _merged_row(1)],
    )
    assert [r.row_index for r in page.rows] == [1, 2]


def test_register_page_input_rejects_duplicate_row_index() -> None:
    with pytest.raises(ValidationError):
        RegisterPageInput(
            page_no=1,
            page_sha256=None,
            month_written="Sep 2026",
            row_count_a=2,
            row_count_b=2,
            blank_slots_a=14,
            blank_slots_b=14,
            rows=[_merged_row(1), _merged_row(1)],
        )


@pytest.mark.parametrize("field", ["row_count_a", "row_count_b", "blank_slots_a", "blank_slots_b"])
def test_register_page_input_counts_bounded_0_to_16(field: str) -> None:
    base = dict(
        page_no=1,
        page_sha256=None,
        month_written="Sep 2026",
        row_count_a=0,
        row_count_b=0,
        blank_slots_a=0,
        blank_slots_b=0,
        rows=[],
    )
    with pytest.raises(ValidationError):
        RegisterPageInput(**(base | {field: 17}))
    with pytest.raises(ValidationError):
        RegisterPageInput(**(base | {field: -1}))
    RegisterPageInput(**(base | {field: 16}))  # upper bound ok


# --- E2: stable_id ---------------------------------------------------------------------------


def test_stable_id_is_deterministic() -> None:
    run_id = uuid4()
    assert stable_id(run_id, "ERP-0001") == stable_id(run_id, "ERP-0001")


def test_stable_id_differs_per_part() -> None:
    run_id = uuid4()
    assert stable_id(run_id, "ERP-0001") != stable_id(run_id, "ERP-0002")


def test_stable_id_differs_per_run() -> None:
    assert stable_id(uuid4(), "ERP-0001") != stable_id(uuid4(), "ERP-0001")


def test_stable_id_returns_uuid() -> None:
    assert isinstance(stable_id(uuid4(), "x"), UUID)


# --- E3: Fault ---------------------------------------------------------------------------------


def test_fault_has_exactly_section_25_values() -> None:
    assert {f.value for f in Fault} == {
        "EXTRACT_TIMEOUT",
        "EXTRACT_BAD_JSON",
        "TELEGRAM_DOWN",
        "PROOF_DROP_ROW",
    }


# --- E4: cell_status canonical keys -----------------------------------------------------------


@pytest.mark.parametrize("source", [Source.ERP, Source.SHEET])
def test_canonical_row_cell_status_needs_all_eight_keys(source: Source) -> None:
    _canonical_row(source, _ALL_CLEAR)  # ok


@pytest.mark.parametrize("source", [Source.ERP, Source.SHEET])
def test_canonical_row_cell_status_rejects_missing_key(source: Source) -> None:
    incomplete = {k: v for k, v in _ALL_CLEAR.items() if k != "uom"}
    with pytest.raises(ValidationError):
        _canonical_row(source, incomplete)


@pytest.mark.parametrize("source", [Source.ERP, Source.SHEET])
def test_canonical_row_cell_status_rejects_extra_key(source: Source) -> None:
    extra = dict(_ALL_CLEAR, unknown_field=CellStatus.CLEAR)
    with pytest.raises(ValidationError):
        _canonical_row(source, extra)
