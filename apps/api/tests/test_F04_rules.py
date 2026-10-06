"""F-04 rules R1 to R9 and the normalisation log (contracts/normalise.md N1 to N14)."""

from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.domain.enums import (
    CellStatus,
    FlagType,
    NormCode,
    ReviewKind,
    ReviewReason,
    ReviewStatus,
    RowKind,
    Source,
)
from app.domain.models import CANONICAL_CELL_FIELDS, UnknownMappingContext
from app.pipeline.engine import EngineInputs
from app.pipeline.normalise import NormaliseResult, RawRow, read_erp, read_sheet
from tests.support.normalised import (
    PACK_RUN_ID,
    map_decision,
    normalise_pack,
    row_by_id,
    with_values,
)
from tests.support.pack import build_engine_inputs, resolved_cell


@pytest.fixture(scope="module")
def inputs(pack_dir: Path, ground_truth_dir: Path) -> EngineInputs:
    return build_engine_inputs(pack_dir, ground_truth_dir, PACK_RUN_ID)


@pytest.fixture(scope="module")
def erp(inputs: EngineInputs) -> list[RawRow]:
    return read_erp(inputs.erp_bytes)


@pytest.fixture(scope="module")
def sheet(inputs: EngineInputs) -> list[RawRow]:
    return read_sheet(inputs.sheet_bytes, inputs.month)


@pytest.fixture(scope="module")
def result(inputs: EngineInputs, erp: list[RawRow], sheet: list[RawRow]) -> NormaliseResult:
    return normalise_pack(inputs, erp=erp, sheet=sheet)


def _log(
    result: NormaliseResult, row_id: str, field: str
) -> list[tuple[NormCode, str | None, str | None]]:
    return [
        (e.code, e.before, e.after)
        for e in result.norm_log
        if e.source_row_id == row_id and e.field == field
    ]


def test_F04_R1_row_ids(
    pack_dir: Path, erp: list[RawRow], sheet: list[RawRow], result: NormaliseResult
) -> None:
    assert [r.source_row_id for r in erp] == [f"ERP-{n:04}" for n in range(1, 29)]
    assert [r.line_no for r in erp] == list(range(1, 29))
    assert erp[23].values["qty"] == "1097.28"
    assert erp[23].values["item_desc"] == 'GREY FABRIC 63"'
    assert [r.source_row_id for r in sheet] == [f"XLS-r{n}" for n in range(2, 32)]
    assert sheet[16].values["Date"] == "9/14/2026"
    assert sheet[29].values["Remarks"] == "Returned to supplier (torn)"

    register = [f"REG-p{p}-r{r:02}" for p in (1, 2) for r in range(1, 15)]
    erp_ids = [r.source_row_id for r in erp]
    sheet_ids = [r.source_row_id for r in sheet]
    assert [r.source_row_id for r in result.rows] == register + erp_ids + sheet_ids  # N11
    assert len({r.id for r in result.rows}) == 86
    assert {r.row_kind for r in result.rows} == {RowKind.RECEIPT}  # N12
    assert {r.outcome for r in result.rows} == {None}
    assert {r.group_id for r in result.rows} == {None}


def test_F04_R2_override_applied(inputs: EngineInputs, sheet: list[RawRow]) -> None:
    resolved = resolved_cell(PACK_RUN_ID, "REG-p2-r05", "qty", "66")
    resolved_14 = resolved_cell(PACK_RUN_ID, "REG-p2-r14", "qty", "250")
    unit = map_decision(PACK_RUN_ID, "XLS-r2", "unit", "pcs", Source.SHEET)
    changed = normalise_pack(
        inputs,
        sheet=with_values(sheet, "XLS-r2", UOM="boxes"),
        decisions=[resolved, resolved_14, unit],
    )

    row = row_by_id(changed, "REG-p2-r05")
    assert (row.qty, row.qty_base) == (Decimal("66"), Decimal("66.00"))
    assert row.cell_status["qty"] is CellStatus.CLEAR
    assert row.raw["qty"] == "66"  # the merged (struck-through and rewritten) value
    row_14 = row_by_id(changed, "REG-p2-r14")
    assert (row_14.qty, row_14.cell_status["qty"], row_14.raw["qty"]) == (
        Decimal("250"),
        CellStatus.CLEAR,
        None,
    )
    xls = row_by_id(changed, "XLS-r2")
    assert (xls.uom, xls.base_uom, xls.cell_status["uom"]) == ("PCS", "PCS", CellStatus.CLEAR)
    assert xls.raw["UOM"] == "boxes"
    assert changed.flags == [] and changed.review_items == []

    ignored = [
        resolved.model_copy(update={"status": ReviewStatus.OPEN, "resolved_value": None}),
        resolved_14.model_copy(update={"status": ReviewStatus.CANNOT_DETERMINE, "note": "torn"}),
    ]
    unchanged = normalise_pack(inputs, decisions=ignored)
    for row_id, status in (
        ("REG-p2-r05", CellStatus.CORRECTED),
        ("REG-p2-r14", CellStatus.ILLEGIBLE),
    ):
        row = row_by_id(unchanged, row_id)
        assert (row.qty, row.cell_status["qty"]) == (None, status)


def test_F04_R2_supplier_override_alias_or_code(inputs: EngineInputs, sheet: list[RawRow]) -> None:
    """C24: a resolved supplier is an alias in supplier_map.csv or an exact code S-101..S-107."""
    sheet = with_values(sheet, "XLS-r5", Supplier="Qasr Pckg")
    for value, code in (("qasr pkg", "S-101"), ("S-101", "S-101"), ("S-199", None)):
        decision = map_decision(PACK_RUN_ID, "XLS-r5", "supplier", value, Source.SHEET)
        changed = normalise_pack(inputs, sheet=sheet, decisions=[decision])

        row = row_by_id(changed, "XLS-r5")
        assert row.supplier_code == code, value
        assert row.raw["Supplier"] == "Qasr Pckg"
        expected = CellStatus.CLEAR if code else CellStatus.UNCLEAR
        assert row.cell_status["supplier_code"] is expected
        assert [f.flag for f in changed.flags] == ([] if code else [FlagType.UNKNOWN_SUPPLIER])

    register = resolved_cell(PACK_RUN_ID, "REG-p1-r01", "supplier", "S-106")
    changed = normalise_pack(inputs, decisions=[register])
    assert row_by_id(changed, "REG-p1-r01").supplier_code == "S-106"


def test_F04_R2_date_override_uses_run_month(inputs: EngineInputs, sheet: list[RawRow]) -> None:
    """data-model Section 6: a resolved date is `DD/MM` with the year from the run month."""
    sheet = with_values(sheet, "XLS-r18", Date="14-09-2026")
    decision = map_decision(PACK_RUN_ID, "XLS-r18", "date", "14/09", Source.SHEET)
    changed = normalise_pack(inputs, sheet=sheet, decisions=[decision])

    row = row_by_id(changed, "XLS-r18")
    assert (row.date, row.cell_status["date"]) == (date(2026, 9, 14), CellStatus.CLEAR)
    assert changed.review_items == []


def test_F04_R3_grn_prefix_and_length(
    inputs: EngineInputs, erp: list[RawRow], result: NormaliseResult
) -> None:
    assert row_by_id(result, "ERP-0001").grn_no == 4401
    assert _log(result, "ERP-0001", "grn_no") == [
        (NormCode.GRN_PREFIX_STRIPPED, "GRN-4401", "4401")
    ]
    assert row_by_id(result, "REG-p1-r01").grn_no == 4401
    assert _log(result, "REG-p1-r01", "grn_no") == []
    assert row_by_id(result, "XLS-r2").grn_no == 4401
    assert _log(result, "XLS-r2", "grn_no") == []
    assert row_by_id(result, "XLS-r31").grn_no is None  # empty GRN cell: nothing to log
    assert _log(result, "XLS-r31", "grn_no") == []

    erp = with_values(erp, "ERP-0002", grn_no="GRN- 4402")
    erp = with_values(erp, "ERP-0003", grn_no="GRN-44")
    erp = with_values(erp, "ERP-0004", grn_no="GRN-1234567")
    erp = with_values(erp, "ERP-0005", grn_no=" 4405 ")
    erp = with_values(erp, "ERP-0006", grn_no="GRN-44O6")
    changed = normalise_pack(inputs, erp=erp)

    assert row_by_id(changed, "ERP-0002").grn_no == 4402
    assert _log(changed, "ERP-0002", "grn_no") == [
        (NormCode.GRN_PREFIX_STRIPPED, "GRN- 4402", "4402")
    ]
    for row_id, raw in (
        ("ERP-0003", "GRN-44"),
        ("ERP-0004", "GRN-1234567"),
        ("ERP-0006", "GRN-44O6"),
    ):
        assert row_by_id(changed, row_id).grn_no is None
        assert _log(changed, row_id, "grn_no") == [(NormCode.GRN_PREFIX_STRIPPED, raw, None)]
    assert row_by_id(changed, "ERP-0005").grn_no == 4405
    assert _log(changed, "ERP-0005", "grn_no") == [(NormCode.TEXT_TRIMMED, " 4405 ", "4405")]


def test_F04_R4_register_year_from_header(inputs: EngineInputs, result: NormaliseResult) -> None:
    assert row_by_id(result, "REG-p1-r01").date == date(2026, 9, 1)
    assert _log(result, "REG-p1-r01", "date") == [
        (NormCode.DATE_YEAR_FROM_HEADER, "01/09", "2026-09-01")
    ]

    p1, p2 = inputs.register_pages
    pages = [
        p1.model_copy(update={"month_written": "Sep 2025"}),
        p2.model_copy(update={"month_written": None}),  # unreadable header: run month
    ]
    changed = normalise_pack(inputs, pages=pages)

    assert row_by_id(changed, "REG-p1-r01").date == date(2025, 9, 1)
    assert _log(changed, "REG-p1-r01", "date") == [
        (NormCode.DATE_YEAR_FROM_HEADER, "01/09", "2025-09-01")
    ]
    assert row_by_id(changed, "REG-p2-r14").date == date(2026, 9, 28)
    assert _log(changed, "REG-p2-r14", "date") == [
        (NormCode.DATE_YEAR_FROM_HEADER, "28/09", "2026-09-28")
    ]


def test_F04_R4_sheet_datetime_cell(
    inputs: EngineInputs, sheet: list[RawRow], result: NormaliseResult
) -> None:
    """C2: openpyxl `datetime` → date part, code DATE_SERIAL_PARSED; a time stays in `raw`."""
    assert row_by_id(result, "XLS-r3").date == date(2026, 9, 1)
    assert _log(result, "XLS-r3", "date") == [
        (NormCode.DATE_SERIAL_PARSED, "2026-09-01 00:00:00", "2026-09-01")
    ]

    sheet = with_values(sheet, "XLS-r3", Date=datetime(2026, 9, 1, 14, 30))  # noqa: DTZ001
    sheet = with_values(sheet, "XLS-r4", Date=46267)
    changed = normalise_pack(inputs, sheet=sheet)

    row = row_by_id(changed, "XLS-r3")
    assert row.date == date(2026, 9, 1)
    assert row.raw["Date"] == "2026-09-01 14:30:00"
    assert row.cell_status["date"] is CellStatus.CLEAR
    assert row_by_id(changed, "XLS-r4").date == date(2026, 9, 2)
    assert _log(changed, "XLS-r4", "date") == [(NormCode.DATE_SERIAL_PARSED, "46267", "2026-09-02")]


def test_F04_R4_unparseable_text_date(inputs: EngineInputs, sheet: list[RawRow]) -> None:
    sheet = with_values(sheet, "XLS-r18", Date="14-09-2026")
    changed = normalise_pack(inputs, sheet=sheet)

    row = row_by_id(changed, "XLS-r18")
    assert row.date is None
    assert row.cell_status["date"] is CellStatus.UNCLEAR
    assert row.raw["Date"] == "14-09-2026"
    (item,) = changed.review_items
    assert item.item_key == "MAP:XLS-r18:date"
    assert (item.kind, item.reason, item.field) == (
        ReviewKind.UNKNOWN_MAPPING,
        ReviewReason.UNPARSEABLE_DATE,
        "date",
    )
    assert item.context == UnknownMappingContext(source=Source.SHEET, raw="14-09-2026")
    assert (item.grn_no, item.source_row_id) == (4416, "XLS-r18")
    assert changed.flags == []  # no flag type for dates; the review item is enough


def test_F04_R5_erp_code_outside_list(inputs: EngineInputs, erp: list[RawRow]) -> None:
    changed = normalise_pack(inputs, erp=with_values(erp, "ERP-0001", supplier_code="S-108"))

    row = row_by_id(changed, "ERP-0001")
    assert row.supplier_code is None
    assert row.cell_status["supplier_code"] is CellStatus.UNCLEAR
    (flag,) = changed.flags
    assert (flag.flag, flag.grn_no, flag.source_row_id, flag.detail) == (
        FlagType.UNKNOWN_SUPPLIER,
        4401,
        "ERP-0001",
        {"raw": "S-108"},
    )
    (item,) = changed.review_items
    assert (item.item_key, item.reason) == ("MAP:ERP-0001:supplier", ReviewReason.UNKNOWN_SUPPLIER)
    assert item.context == UnknownMappingContext(source=Source.ERP, raw="S-108")


def test_F04_R6_unknown_unit(inputs: EngineInputs, sheet: list[RawRow]) -> None:
    changed = normalise_pack(inputs, sheet=with_values(sheet, "XLS-r2", UOM="boxes"))

    row = row_by_id(changed, "XLS-r2")
    assert row.qty == Decimal("2000")
    assert (row.uom, row.qty_base, row.base_uom) == (None, None, None)
    assert row.cell_status["uom"] is CellStatus.UNCLEAR
    assert [(f.flag, f.detail) for f in changed.flags] == [
        (FlagType.UNKNOWN_UNIT, {"raw": "boxes"})
    ]
    (item,) = changed.review_items
    assert (item.item_key, item.reason) == ("MAP:XLS-r2:unit", ReviewReason.UNKNOWN_UNIT)


def test_F04_R7_round_half_up(inputs: EngineInputs, erp: list[RawRow], sheet: list[RawRow]) -> None:
    erp = with_values(erp, "ERP-0024", qty="1097.285")
    sheet = with_values(sheet, "XLS-r27", **{"Qty Received": 2.345})
    changed = normalise_pack(inputs, erp=erp, sheet=sheet)

    row = row_by_id(changed, "ERP-0024")
    assert row.qty == Decimal("1097.285")
    assert row.qty_base == Decimal("1097.29")  # half-even would give 1097.28
    sheet_row = row_by_id(changed, "XLS-r27")
    assert sheet_row.qty == Decimal("2.345")  # float cell via str(), never Decimal(float)
    assert sheet_row.qty_base == Decimal("2.35")
    assert _log(changed, "ERP-0024", "qty") == []  # parsing is not a change


def test_F04_R8_lot_upper_item_display(
    inputs: EngineInputs, sheet: list[RawRow], result: NormaliseResult
) -> None:
    assert row_by_id(result, "REG-p1-r08").item == "H202 50%"  # kept as written, never compared
    assert row_by_id(result, "ERP-0024").item == 'GREY FABRIC 63"'
    assert _log(result, "REG-p1-r08", "item") == []

    sheet = with_values(sheet, "XLS-r2", Lot=" l2609-001 ", Material="  Cartons 5-ply ")
    changed = normalise_pack(inputs, sheet=sheet)

    row = row_by_id(changed, "XLS-r2")
    assert (row.lot_no, row.item) == ("L2609-001", "Cartons 5-ply")
    assert _log(changed, "XLS-r2", "lot_no") == [
        (NormCode.TEXT_TRIMMED, " l2609-001 ", "L2609-001")
    ]
    assert _log(changed, "XLS-r2", "item") == [
        (NormCode.TEXT_TRIMMED, "  Cartons 5-ply ", "Cartons 5-ply")
    ]


def test_F04_R9_unresolved_cells_null(result: NormaliseResult) -> None:
    corrected = row_by_id(result, "REG-p2-r05")
    assert corrected.qty is None and corrected.qty_base is None
    assert corrected.cell_status["qty"] is CellStatus.CORRECTED
    assert corrected.raw["qty"] == "66"
    assert (corrected.uom, corrected.base_uom) == ("DRUM", "DRUM")
    illegible = row_by_id(result, "REG-p2-r14")
    assert illegible.qty is None and illegible.qty_base is None
    assert illegible.cell_status["qty"] is CellStatus.ILLEGIBLE
    assert illegible.raw["qty"] is None
    assert illegible.cell_status["date"] is CellStatus.CLEAR


def test_F04_normlog_one_entry_per_change(result: NormaliseResult) -> None:
    keys = [(e.source_row_id, e.field) for e in result.norm_log]
    assert len(keys) == len(set(keys))
    assert all(e.before != e.after for e in result.norm_log)
    assert len({e.id for e in result.norm_log}) == len(result.norm_log)

    def count(prefix: str) -> Counter[tuple[str, NormCode]]:
        return Counter(
            (e.field, e.code) for e in result.norm_log if e.source_row_id.startswith(prefix)
        )

    assert count("REG") == {
        ("date", NormCode.DATE_YEAR_FROM_HEADER): 28,
        ("supplier_code", NormCode.SUPPLIER_MAPPED): 28,
        ("uom", NormCode.UNIT_MAPPED): 28,
        ("qty_base", NormCode.UNIT_CONVERTED): 1,  # REG-p2-r11, 1200 yds
    }
    assert count("ERP") == {("grn_no", NormCode.GRN_PREFIX_STRIPPED): 28}
    assert count("XLS") == {
        ("date", NormCode.DATE_SERIAL_PARSED): 29,
        ("date", NormCode.DATE_TEXT_PARSED): 1,
        ("supplier_code", NormCode.SUPPLIER_MAPPED): 30,
        ("uom", NormCode.UNIT_MAPPED): 30,
    }
    assert _log(result, "REG-p2-r11", "qty_base") == [(NormCode.UNIT_CONVERTED, "1200", "1097.28")]
    assert _log(result, "REG-p2-r11", "uom") == [(NormCode.UNIT_MAPPED, "yds", "YD")]
    assert _log(result, "XLS-r2", "supplier_code") == [
        (NormCode.SUPPLIER_MAPPED, "Qasr Packaging", "S-101")
    ]


def test_F04_row_count_86(result: NormaliseResult) -> None:
    assert Counter(r.source for r in result.rows) == {
        Source.REGISTER: 28,
        Source.ERP: 28,
        Source.SHEET: 30,
    }
    for row in result.rows:  # N14 (E4): every source carries every canonical key
        assert set(row.cell_status) == CANONICAL_CELL_FIELDS


def test_F04_meta_filled(result: NormaliseResult) -> None:
    """N13: register page/row/received_by; ERP entered_by/entered_at (Asia/Karachi); remarks."""
    reg = row_by_id(result, "REG-p2-r11").meta
    assert (reg.page_no, reg.row_index, reg.received_by) == (2, 11, "B.H.")
    erp = row_by_id(result, "ERP-0028").meta
    assert erp.entered_by == "admin2"
    assert erp.entered_at is not None
    assert erp.entered_at.isoformat() == "2026-10-02T18:47:00+05:00"
    assert row_by_id(result, "XLS-r31").meta.remarks == "Returned to supplier (torn)"
    assert row_by_id(result, "XLS-r2").meta.remarks is None
    erp_row = row_by_id(result, "ERP-0024")
    assert (erp_row.qty, erp_row.uom, erp_row.qty_base, erp_row.base_uom) == (
        Decimal("1097.28"),
        "M",
        Decimal("1097.28"),
        "M",
    )
    assert erp_row.raw["grn_no"] == "GRN-4425"
    assert erp_row.date == date(2026, 9, 24)
