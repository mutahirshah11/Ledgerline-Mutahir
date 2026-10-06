"""F-04 normalisation (contracts/normalise.md). Pure; the only I/O is reading the shipped
mapping files in `load_mappings`. No clock use."""

import csv
import io
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import NamedTuple, TypeVar

import openpyxl

from app.domain.ids import erp_row_id, sheet_row_id

MAPPINGS_DIR = Path(__file__).resolve().parents[1] / "mappings"

_SUPPLIER_HEADER = ["alias", "code"]
_UNIT_HEADER = ["alias", "unit", "base", "factor"]
_SUPPLIER_CODE = re.compile(r"S-10[1-7]")

V = TypeVar("V")


@dataclass(frozen=True)
class UnitDef:
    """A canonical unit and its conversion to the base unit (F-04.R6, R7)."""

    unit: str
    base: str
    factor: Decimal


@dataclass(frozen=True)
class MappingTables:
    """Alias lookups from the shipped mapping files (F-04.R5, R6; research R5).

    Keys are aliases trimmed, inner whitespace collapsed and casefolded; lookup is exact
    (no fuzzy matching, constitution V).
    """

    suppliers: Mapping[str, str]
    codes: frozenset[str]
    units: Mapping[str, UnitDef]


def _alias_key(value: str) -> str:
    """Research R5 lookup key: trim, collapse inner whitespace, casefold."""
    return " ".join(value.split()).casefold()


def _read_rows(path: Path, header: list[str]) -> list[tuple[int, list[str]]]:
    """(line number, cells) per data line; ValueError on a header other than `header`."""
    with path.open(encoding="utf-8", newline="") as f:
        lines = list(csv.reader(f))
    if not lines or [h.strip() for h in lines[0]] != header:
        raise ValueError(f"{path.name}: header must be {','.join(header)}")
    rows = []
    for line_no, cells in enumerate(lines[1:], start=2):
        if not any(c.strip() for c in cells):
            continue
        if len(cells) != len(header):
            raise ValueError(f"{path.name} line {line_no}: expected {len(header)} columns")
        rows.append((line_no, [c.strip() for c in cells]))
    return rows


def _add_alias(table: dict[str, V], alias: str, value: V, where: str) -> None:
    key = _alias_key(alias)
    if not key:
        raise ValueError(f"{where}: blank alias")
    if key in table:
        raise ValueError(f"{where}: duplicate alias {alias!r}")
    table[key] = value


def load_mappings(dir: Path = MAPPINGS_DIR) -> MappingTables:
    """Read `supplier_map.csv` (alias,code) and `unit_map.csv` (alias,unit,base,factor).

    ValueError on a duplicate alias, an unknown header, a code outside S-101..S-107, a factor
    that is not a positive Decimal, or a base unit not defined as a unit (contracts/normalise.md).
    """
    suppliers: dict[str, str] = {}
    path = dir / "supplier_map.csv"
    for line_no, (alias, code) in _read_rows(path, _SUPPLIER_HEADER):
        where = f"{path.name} line {line_no}"
        if not _SUPPLIER_CODE.fullmatch(code):
            raise ValueError(f"{where}: code {code!r} is not one of S-101..S-107")
        _add_alias(suppliers, alias, code, where)

    units: dict[str, UnitDef] = {}
    path = dir / "unit_map.csv"
    for line_no, (alias, unit, base, factor_text) in _read_rows(path, _UNIT_HEADER):
        where = f"{path.name} line {line_no}"
        try:
            factor = Decimal(factor_text)
        except InvalidOperation:
            raise ValueError(f"{where}: factor {factor_text!r} is not a decimal") from None
        if not factor.is_finite() or factor <= 0 or not unit or not base:
            raise ValueError(f"{where}: need unit, base and a positive factor")
        _add_alias(units, alias, UnitDef(unit=unit, base=base, factor=factor), where)

    undefined = sorted({u.base for u in units.values()} - {u.unit for u in units.values()})
    if undefined:
        raise ValueError(f"{path.name}: base unit(s) {undefined} not defined as a unit")

    return MappingTables(
        suppliers=MappingProxyType(suppliers),
        codes=frozenset(suppliers.values()),
        units=MappingProxyType(units),
    )


# --- Source readers (F-04.R1; research R2, R3) ---------------------------------------------

ERP_COLUMNS = (
    "grn_no",
    "grn_date",
    "supplier_code",
    "supplier_name",
    "item_desc",
    "lot_no",
    "qty",
    "uom",
    "entered_by",
    "entered_at",
)
SHEET_HEADERS = ("Date", "GRN", "Supplier", "Material", "Lot", "Qty Received", "UOM", "Remarks")
_MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_EXCEL_EPOCH = date(1899, 12, 30)


class RawRow(NamedTuple):
    """One source row as read, before normalisation (contracts/normalise.md).

    ERP values are `str | None`; SHEET values keep openpyxl types until normalised.
    `line_no` is the ERP data line (1 = first data row) or the Excel row number.
    """

    source_row_id: str
    values: dict[str, object]
    line_no: int


def excel_serial_to_date(serial: int | Decimal) -> date:
    """Excel serial day number to a date, base 1899-12-30 (F-04.R4, AC5: 46292 → 2026-09-27).

    ValueError for a non-integral or out-of-range (< 1) serial.
    """
    if isinstance(serial, bool) or not isinstance(serial, int | Decimal):
        raise ValueError(f"serial must be int or Decimal, got {serial!r}")
    if serial != int(serial) or serial < 1:
        raise ValueError(f"serial must be an integer >= 1, got {serial!r}")
    try:
        return _EXCEL_EPOCH + timedelta(days=int(serial))
    except OverflowError:
        raise ValueError(f"serial out of range: {serial!r}") from None


def read_erp(data: bytes) -> list[RawRow]:
    """ERP CSV rows (Section 7): UTF-8 with optional BOM, header row, `ERP-{line:04}` ids with
    line 1 = first data row (F-04.R1; research R2). Values as written ("" → None).

    ValueError naming the line on a contract break (normally caught earlier by F-02).
    """
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise ValueError(f"ERP CSV is not UTF-8: {e}") from None
    reader = csv.DictReader(io.StringIO(text, newline=""))
    missing = [c for c in ERP_COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise ValueError(f"ERP CSV header is missing column(s) {missing}")
    rows = []
    for line_no, record in enumerate(reader, start=1):
        if None in record or any(record[c] is None for c in ERP_COLUMNS):
            raise ValueError(f"ERP CSV line {line_no}: wrong number of values")
        values: dict[str, object] = {c: (record[c] or None) for c in ERP_COLUMNS}
        rows.append(RawRow(erp_row_id(line_no), values, line_no))
    return rows


def sheet_name(month: str) -> str:
    """Worksheet name `Mon-YYYY` for a run month `YYYY-MM` (`2026-09` → `Sep-2026`), locale-free."""
    year, mon = month.split("-")
    return f"{_MONTH_ABBR[int(mon) - 1]}-{year}"


def read_sheet(data: bytes, month: str) -> list[RawRow]:
    """Store sheet rows (Section 7): worksheet `Mon-YYYY`, header in row 1, rows 2 to the last row
    with any non-empty cell, `XLS-r{n}` ids (F-04.R1; research R3). Values keep openpyxl types.

    ValueError on a missing worksheet or header (normally caught earlier by F-02).
    """
    name = sheet_name(month)
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        if name not in wb.sheetnames:
            raise ValueError(f"store sheet has no worksheet {name!r}")
        cells = [list(r) for r in wb[name].iter_rows(values_only=True)]
    finally:
        wb.close()
    header = [str(h).strip() if h is not None else "" for h in (cells[0] if cells else [])]
    missing = [h for h in SHEET_HEADERS if h not in header]
    if missing:
        raise ValueError(f"worksheet {name!r} row 1 is missing header(s) {missing}")
    index = {h: header.index(h) for h in SHEET_HEADERS}

    def _empty(v: object) -> bool:
        return v is None or (isinstance(v, str) and not v.strip())

    last = max(
        (n for n, r in enumerate(cells, start=1) if n >= 2 and not all(map(_empty, r))), default=1
    )
    rows = []
    for excel_row in range(2, last + 1):
        record = cells[excel_row - 1]
        values: dict[str, object] = {
            h: (record[i] if i < len(record) else None) for h, i in index.items()
        }
        rows.append(RawRow(sheet_row_id(excel_row), values, excel_row))
    return rows
