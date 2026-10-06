"""F-04 acceptance criteria (blueprint Section 16; spec US1). The pack is parsed once per module."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.domain.enums import CellStatus, FlagType, NormCode, ReviewKind, ReviewReason
from app.domain.models import CanonicalRow
from app.pipeline.engine import EngineInputs
from app.pipeline.normalise import NormaliseResult, excel_serial_to_date, read_sheet
from tests.support.normalised import PACK_RUN_ID, normalise_pack, row_by_id, with_values
from tests.support.pack import build_engine_inputs


@pytest.fixture(scope="module")
def inputs(pack_dir: Path, ground_truth_dir: Path) -> EngineInputs:
    return build_engine_inputs(pack_dir, ground_truth_dir, PACK_RUN_ID)


@pytest.fixture(scope="module")
def result(inputs: EngineInputs) -> NormaliseResult:
    return normalise_pack(inputs)


def _row(result: NormaliseResult, row_id: str) -> CanonicalRow:
    return row_by_id(result, row_id)


def test_F04_AC1_text_date_parsed(result: NormaliseResult) -> None:
    assert _row(result, "XLS-r18").date == date(2026, 9, 14)
    entries = [e for e in result.norm_log if e.source_row_id == "XLS-r18" and e.field == "date"]
    assert len(entries) == 1
    assert entries[0].code is NormCode.DATE_TEXT_PARSED
    assert (entries[0].before, entries[0].after) == ("9/14/2026", "2026-09-14")


def test_F04_AC2_yards_converted(result: NormaliseResult) -> None:
    reg = _row(result, "REG-p2-r11")
    assert (reg.qty, reg.uom) == (Decimal("1200"), "YD")
    assert (reg.qty_base, reg.base_uom) == (Decimal("1097.28"), "M")
    xls = _row(result, "XLS-r27")
    assert (xls.qty_base, xls.base_uom) == (Decimal("1200.00"), "M")
    assert str(xls.qty_base) == "1200.00"


def test_F04_AC3_qasr_aliases(result: NormaliseResult) -> None:
    assert _row(result, "REG-p1-r01").raw["supplier"] == "Qasr Pkg"
    assert _row(result, "XLS-r2").raw["Supplier"] == "Qasr Packaging"
    assert _row(result, "ERP-0001").raw["supplier_code"] == "S-101"
    for row_id in ("REG-p1-r01", "XLS-r2", "ERP-0001"):
        assert _row(result, row_id).supplier_code == "S-101"


def test_F04_AC4_typo_unknown_supplier(inputs: EngineInputs) -> None:
    sheet = read_sheet(inputs.sheet_bytes, inputs.month)
    sheet = with_values(sheet, "XLS-r5", Supplier="Qasr Pckg")

    result = normalise_pack(inputs, sheet=sheet)

    row = _row(result, "XLS-r5")
    assert row.supplier_code is None
    assert row.cell_status["supplier_code"] is CellStatus.UNCLEAR
    flags = [f for f in result.flags if f.source_row_id == "XLS-r5"]
    assert [(f.flag, f.detail) for f in flags] == [
        (FlagType.UNKNOWN_SUPPLIER, {"raw": "Qasr Pckg"})
    ]
    items = [i for i in result.review_items if i.item_key == "MAP:XLS-r5:supplier"]
    assert len(items) == 1
    assert items[0].kind is ReviewKind.UNKNOWN_MAPPING
    assert items[0].reason is ReviewReason.UNKNOWN_SUPPLIER


def test_F04_AC5_serial_46292() -> None:
    assert excel_serial_to_date(46292) == date(2026, 9, 27)
    assert excel_serial_to_date(46266) == date(2026, 9, 1)


def test_F04_AC6_no_float(result: NormaliseResult) -> None:
    assert len(result.rows) == 86
    for row in result.rows:
        for value in (row.qty, row.qty_base):
            assert value is None or type(value) is Decimal, (row.source_row_id, value)


def test_F04_pack_has_no_unknown_mappings(result: NormaliseResult) -> None:
    """The pack normalises with no UNKNOWN_* flag and no review item (Section 8)."""
    assert result.flags == []
    assert result.review_items == []
