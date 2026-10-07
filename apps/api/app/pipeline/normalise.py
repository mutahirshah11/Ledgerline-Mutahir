"""F-04 normalisation (contracts/normalise.md). Pure; the only I/O is reading the shipped
mapping files in `load_mappings`. No clock use."""

import csv
import io
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import NamedTuple, TypeVar
from uuid import UUID

import openpyxl

from app.core.clock import KARACHI
from app.domain.enums import (
    CellStatus,
    FlagType,
    NormCode,
    RegisterField,
    ReviewKind,
    ReviewReason,
    ReviewStatus,
    RowKind,
    Source,
)
from app.domain.ids import erp_row_id, register_row_id, review_item_key, sheet_row_id, stable_id
from app.domain.models import (
    CANONICAL_CELL_FIELDS,
    CanonicalRow,
    ControlFlag,
    NormLogEntry,
    RegisterPageInput,
    ReviewItem,
    RowMeta,
    UnknownMappingContext,
)

MAPPINGS_DIR = Path(__file__).resolve().parents[1] / "mappings"

_SUPPLIER_HEADER = ["alias", "code"]
_UNIT_HEADER = ["alias", "unit", "base", "factor"]
SUPPLIER_CODES = frozenset(f"S-{n}" for n in range(101, 108))
"""The only valid supplier codes, S-101..S-107 (Section 16; research R5)."""

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

    def supplier_code(self, text: str) -> str | None:
        """The code for a supplier alias (research R5 key), or None."""
        return self.suppliers.get(_alias_key(text))

    def unit_def(self, text: str) -> UnitDef | None:
        """The unit for a unit alias (research R5 key), or None."""
        return self.units.get(_alias_key(text))


def _alias_key(value: str) -> str:
    """Research R5 lookup key: trim, collapse inner whitespace, casefold."""
    return " ".join(value.split()).casefold()


def _read_rows(path: Path, header: list[str]) -> list[tuple[int, list[str]]]:
    """(line number, cells) per data line; ValueError on a header other than `header`."""
    with path.open(encoding="utf-8-sig", newline="") as f:
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


def load_mappings(mappings_dir: Path = MAPPINGS_DIR) -> MappingTables:
    """Read `supplier_map.csv` (alias,code) and `unit_map.csv` (alias,unit,base,factor).

    ValueError on a duplicate alias, an unknown header, a code outside S-101..S-107, a factor
    that is not a positive Decimal, a base unit not defined as a unit (contracts/normalise.md),
    aliases of one unit with different base or factor, a base unit with factor other than 1, or
    a base unit that is not its own base (no chains). `codes` is always the fixed list
    S-101..S-107.
    """
    suppliers: dict[str, str] = {}
    path = mappings_dir / "supplier_map.csv"
    for line_no, (alias, code) in _read_rows(path, _SUPPLIER_HEADER):
        where = f"{path.name} line {line_no}"
        if code not in SUPPLIER_CODES:
            raise ValueError(f"{where}: code {code!r} is not one of S-101..S-107")
        _add_alias(suppliers, alias, code, where)

    units: dict[str, UnitDef] = {}
    path = mappings_dir / "unit_map.csv"
    for line_no, (alias, unit, base, factor_text) in _read_rows(path, _UNIT_HEADER):
        where = f"{path.name} line {line_no}"
        try:
            factor = Decimal(factor_text)
        except InvalidOperation:
            raise ValueError(f"{where}: factor {factor_text!r} is not a decimal") from None
        if not factor.is_finite() or factor <= 0 or not unit or not base:
            raise ValueError(f"{where}: need unit, base and a positive factor")
        unit_def = UnitDef(unit=unit, base=base, factor=factor)
        if unit == base and factor != 1:
            raise ValueError(f"{where}: base unit {base} needs factor 1")
        known = next((u for u in units.values() if u.unit == unit), None)
        if known is not None and known != unit_def:
            raise ValueError(f"{where}: aliases of {unit} disagree on base or factor")
        _add_alias(units, alias, unit_def, where)

    by_unit = {u.unit: u for u in units.values()}
    undefined = sorted({u.base for u in units.values()} - by_unit.keys())
    if undefined:
        raise ValueError(f"{path.name}: base unit(s) {undefined} not defined as a unit")
    chained = sorted({u.base for u in units.values() if by_unit[u.base].base != u.base})
    if chained:
        raise ValueError(f"{path.name}: base unit(s) {chained} not its own base")

    return MappingTables(
        suppliers=MappingProxyType(suppliers),
        codes=SUPPLIER_CODES,
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
    if isinstance(serial, Decimal) and not serial.is_finite():
        raise ValueError(f"serial must be finite, got {serial!r}")
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
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:  # BadZipFile, KeyError, InvalidFileException, ...: varies by damage
        raise ValueError(f"store sheet is not a readable .xlsx: {e}") from None
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
    last = max(
        (n for n, r in enumerate(cells, start=1) if n >= 2 and not all(map(_blank, r))), default=1
    )
    rows = []
    for excel_row in range(2, last + 1):
        record = cells[excel_row - 1]
        values: dict[str, object] = {
            h: (record[i] if i < len(record) else None) for h, i in index.items()
        }
        rows.append(RawRow(sheet_row_id(excel_row), values, excel_row))
    return rows


# --- Field normalisation (F-04.R3 to R9; data-model.md Section 3) ---------------------------

REGISTER_TO_CANONICAL: Mapping[RegisterField, str] = MappingProxyType(
    {
        RegisterField.DATE: "date",
        RegisterField.GRN_NO: "grn_no",
        RegisterField.SUPPLIER: "supplier_code",
        RegisterField.ITEM: "item",
        RegisterField.LOT_NO: "lot_no",
        RegisterField.QTY: "qty",
        RegisterField.UNIT: "uom",
        RegisterField.RECD_BY: "received_by",
    }
)
"""Register field → canonical field (data-model.md Section 1)."""

_ERP_TO_CANONICAL = {
    "grn_no": "grn_no",
    "grn_date": "date",
    "supplier_code": "supplier_code",
    "item_desc": "item",
    "lot_no": "lot_no",
    "qty": "qty",
    "uom": "uom",
}
_SHEET_TO_CANONICAL = {
    "Date": "date",
    "GRN": "grn_no",
    "Supplier": "supplier_code",
    "Material": "item",
    "Lot": "lot_no",
    "Qty Received": "qty",
    "UOM": "uom",
}
_GRN_DIGITS = re.compile(r"[0-9]{3,6}")  # ASCII only: Arabic-Indic digits are not a GRN
_REGISTER_DATE = re.compile(r"(\d{1,2})/(\d{1,2})")
_SHEET_TEXT_DATE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
_ISO_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_CENT = Decimal("0.01")
_QTY_MAX_EXPONENT = 15
"""A qty with 15 or more integer digits is out of range and read as unparseable."""
_MAP_FIELDS = {"date": "date", "supplier_code": "supplier", "uom": "unit"}
"""Canonical field → `MAP:{row}:{field}` review field (data-model.md Section 6)."""


@dataclass(frozen=True)
class NormaliseResult:
    """F-04 output (data-model.md Section 2): rows in N11 order, outcome and group unset."""

    rows: list[CanonicalRow]
    norm_log: list[NormLogEntry]
    flags: list[ControlFlag]  # UNKNOWN_SUPPLIER / UNKNOWN_UNIT only
    review_items: list[ReviewItem]  # UNKNOWN_MAPPING, status OPEN


@dataclass
class _Cell:
    """One canonical field's input: the value as read and its cell status."""

    value: object
    status: CellStatus = CellStatus.CLEAR


@dataclass
class _Out:
    """Run-wide accumulators, filled in row order."""

    run_id: UUID
    run_year: int
    mappings: MappingTables
    overrides: Mapping[str, str]  # item_key → resolved value of RESOLVED decisions (F-04.R2)
    norm_log: list[NormLogEntry] = field(default_factory=list)
    flags: list[ControlFlag] = field(default_factory=list)
    review_items: list[ReviewItem] = field(default_factory=list)


def _text(value: object) -> str | None:
    """Raw text of a value as read; openpyxl floats via `str` (shortest round-trip, research R1)."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _int_if_whole_float(value: object) -> object:
    """An openpyxl float holding a whole number (`4401.0`) as `int`; anything else unchanged."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _date_or_none(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _header_year(month_written: str | None) -> int | None:
    """Year of a register month header written `Mon YYYY` ("Sep 2026"); None if unreadable."""
    parts = (month_written or "").split()
    abbrs = [m.casefold() for m in _MONTH_ABBR]
    if len(parts) == 2 and parts[0].casefold() in abbrs and re.fullmatch(r"\d{4}", parts[1]):
        return int(parts[1])
    return None


class _RowNormaliser:
    """Normalises one source row into a `CanonicalRow`, logging every changed value (F-04)."""

    def __init__(
        self, out: _Out, source: Source, row_id: str, cells: dict[str, _Cell], year: int
    ) -> None:
        self.out = out
        self.source = source
        self.row_id = row_id
        self.cells = dict(cells)
        self.year = year  # year for register `DD/MM` dates
        self.overridden: set[str] = set()
        self._apply_overrides()
        self.status = {
            name: self.cells[name].status if name in self.cells else CellStatus.CLEAR
            for name in sorted(CANONICAL_CELL_FIELDS)
        }
        self.grn_no = self._grn()  # before the other fields: flags and review items carry it

    def _apply_overrides(self) -> None:
        """F-04.R2, before any other step: a `RESOLVED` `CELL:{row}:{register_field}`,
        `MAP:{row}:{field}` or `ROW:{row}` (GRN) decision replaces the value and makes the cell
        `CLEAR`; `raw` keeps the original. A MAP decision is applied after a CELL one for the
        same field."""
        keys: list[tuple[str, str]] = []
        if self.source is Source.REGISTER:
            keys += [
                (name, review_item_key(ReviewKind.CELL, row_id=self.row_id, field=f.value))
                for f, name in REGISTER_TO_CANONICAL.items()
            ]
        keys += [
            (name, review_item_key(ReviewKind.UNKNOWN_MAPPING, row_id=self.row_id, field=f))
            for name, f in _MAP_FIELDS.items()
        ]
        # 005 data-model §1: an entered GRN on `ROW:{row}` (UNMATCHED_NO_KEY) sets the GRN
        keys.append(("grn_no", review_item_key(ReviewKind.UNMATCHED_ROW, row_id=self.row_id)))
        for name, key in keys:
            if key in self.out.overrides:
                self.cells[name] = _Cell(self.out.overrides[key])
                self.overridden.add(name)

    def _readable(self, name: str) -> bool:
        """F-04.R9: only a `CLEAR` cell gives a canonical value."""
        cell = self.cells.get(name)
        return cell is not None and cell.status is CellStatus.CLEAR

    def _value(self, name: str) -> object:
        return self.cells[name].value if self._readable(name) else None

    def received_by(self) -> str | None:
        """Register `Recd By` text when `CLEAR` (F-04.R9), for `meta.received_by`."""
        return _text(self._value("received_by"))

    def _log(self, name: str, code: NormCode, before: str | None, after: str | None) -> None:
        run_id = self.out.run_id
        self.out.norm_log.append(
            NormLogEntry(
                id=stable_id(run_id, self.row_id, name, code.value),
                run_id=run_id,
                source_row_id=self.row_id,
                field=name,
                code=code,
                before=before,
                after=after,
            )
        )

    def _unknown(
        self, name: str, map_field: str, reason: ReviewReason, flag: FlagType | None
    ) -> None:
        """Clarification 3: `UNCLEAR`, a flag for supplier/unit, an `UNKNOWN_MAPPING` item."""
        raw_text = _text(self.cells[name].value) or ""
        run_id = self.out.run_id
        self.status[name] = CellStatus.UNCLEAR
        if flag is not None:
            self.out.flags.append(
                ControlFlag(
                    id=stable_id(run_id, flag.value, self.row_id),
                    run_id=run_id,
                    flag=flag,
                    grn_no=self.grn_no,
                    source_row_id=self.row_id,
                    detail={"raw": raw_text},
                )
            )
        key = review_item_key(ReviewKind.UNKNOWN_MAPPING, row_id=self.row_id, field=map_field)
        self.out.review_items.append(
            ReviewItem(
                id=stable_id(run_id, key),
                run_id=run_id,
                kind=ReviewKind.UNKNOWN_MAPPING,
                item_key=key,
                grn_no=self.grn_no,
                source_row_id=self.row_id,
                field=map_field,
                reason=reason,
                read_a=None,
                read_b=None,
                context=UnknownMappingContext(source=self.source, raw=raw_text),
            )
        )

    def _grn(self) -> int | None:
        """F-04.R3: strip `GRN-` and spaces; 3 to 6 digits, else null plus a log entry."""
        value = _int_if_whole_float(self._value("grn_no"))
        if _blank(value) or isinstance(value, bool):
            return None
        text = _text(value) or ""
        body = text.strip()
        digits = "".join(body.removeprefix("GRN-").split())
        if not _GRN_DIGITS.fullmatch(digits):
            self._log("grn_no", NormCode.GRN_PREFIX_STRIPPED, text, None)
            return None
        grn_no = int(digits)
        if grn_no < 1:
            # F-04.R3: a GRN of zero is malformed, not a valid GRN (grn_group_key requires >= 1).
            self._log("grn_no", NormCode.GRN_PREFIX_STRIPPED, text, None)
            return None
        if body.startswith("GRN-"):
            self._log("grn_no", NormCode.GRN_PREFIX_STRIPPED, text, str(grn_no))
        elif digits != text:
            self._log("grn_no", NormCode.TEXT_TRIMMED, text, str(grn_no))
        return grn_no

    def row_date(self) -> date | None:
        """F-04.R4; unparseable → null, `UNCLEAR`, review item `UNPARSEABLE_DATE`."""
        if not self._readable("date"):
            return None
        value = self.cells["date"].value
        parsed, code = self._parse_date(value)
        if parsed is None:
            self._unknown("date", "date", ReviewReason.UNPARSEABLE_DATE, None)
            return None
        before, after = _text(value), parsed.isoformat()
        if before != after:
            self._log("date", code, before, after)
        return parsed

    def _parse_date(self, value: object) -> tuple[date | None, NormCode]:
        """Register `DD/MM` + header year; ERP ISO; sheet `datetime` (C2), serial or `M/D/YYYY`.
        A resolved date (F-04.R2) is `DD/MM` with the year of the run month (data-model Section 6).
        """
        text = value.strip() if isinstance(value, str) else None
        if self.source is Source.REGISTER or "date" in self.overridden:
            # A CELL (or MAP) date override takes the run month's year (data-model Section 6).
            year = self.out.run_year if "date" in self.overridden else self.year
            m = _REGISTER_DATE.fullmatch(text or "")
            parsed = _date_or_none(year, int(m[2]), int(m[1])) if m else None
            return parsed, NormCode.DATE_YEAR_FROM_HEADER
        if self.source is Source.ERP:
            m = _ISO_DATE.fullmatch(text or "")
            parsed = _date_or_none(int(m[1]), int(m[2]), int(m[3])) if m else None
            return parsed, NormCode.TEXT_TRIMMED
        if isinstance(value, datetime):
            return value.date(), NormCode.DATE_SERIAL_PARSED
        if isinstance(value, date):
            return value, NormCode.DATE_SERIAL_PARSED
        value = _int_if_whole_float(value)
        if isinstance(value, int) and not isinstance(value, bool):
            try:
                return excel_serial_to_date(value), NormCode.DATE_SERIAL_PARSED
            except ValueError:
                return None, NormCode.DATE_SERIAL_PARSED
        m = _SHEET_TEXT_DATE.fullmatch(text or "")
        parsed = _date_or_none(int(m[3]), int(m[1]), int(m[2])) if m else None
        return parsed, NormCode.DATE_TEXT_PARSED

    def supplier(self) -> str | None:
        """F-04.R5: ERP code as is (must be in `codes`); names by exact alias lookup. A resolved
        supplier (F-04.R2) is an alias, or an exact code in `codes` taken directly (C24)."""
        if not self._readable("supplier_code"):
            return None
        text = _text(self.cells["supplier_code"].value) or ""
        mappings = self.out.mappings
        stripped = text.strip()
        exact_code = stripped if stripped in mappings.codes else None
        if self.source is Source.ERP and "supplier_code" not in self.overridden:
            code = exact_code
            log_code = NormCode.TEXT_TRIMMED
        else:
            code = mappings.supplier_code(text)
            if code is None and "supplier_code" in self.overridden:
                code = exact_code
            log_code = NormCode.SUPPLIER_MAPPED
        if code is None:
            self._unknown(
                "supplier_code",
                "supplier",
                ReviewReason.UNKNOWN_SUPPLIER,
                FlagType.UNKNOWN_SUPPLIER,
            )
            return None
        if code != text:
            self._log("supplier_code", log_code, text, code)
        return code

    def unit(self) -> UnitDef | None:
        """F-04.R6: exact alias lookup in `unit_map.csv`."""
        if not self._readable("uom"):
            return None
        text = _text(self.cells["uom"].value) or ""
        unit = self.out.mappings.unit_def(text)
        if unit is None:
            self._unknown("uom", "unit", ReviewReason.UNKNOWN_UNIT, FlagType.UNKNOWN_UNIT)
            return None
        if unit.unit != text:
            self._log("uom", NormCode.UNIT_MAPPED, text, unit.unit)
        return unit

    def qty(self) -> Decimal | None:
        """F-04.R7: `Decimal`, never from a float (research R1). Parsing is not a change.

        Unparseable (any source; user decision 2026-10-07) → null and `cell_status.qty =
        UNCLEAR`, raw kept, so F-05 rule 1 sends the group to review and C4 lists it unknown.
        """
        value = self._value("qty")
        if _blank(value):
            return None
        text = _text(value) or ""
        try:
            if isinstance(value, bool) or "_" in text:  # no Python `1_000` numerals
                raise InvalidOperation
            qty = Decimal(value) if isinstance(value, int) else Decimal(text)
        except InvalidOperation:
            qty = None
        # Out of range is unreadable too: qty_base must still quantize to 2 dp.
        if qty is None or not qty.is_finite() or qty.adjusted() >= _QTY_MAX_EXPONENT:
            self.status["qty"] = CellStatus.UNCLEAR
            return None
        return qty

    def qty_base(self, qty: Decimal | None, unit: UnitDef | None) -> Decimal | None:
        """F-04.R7: qty × factor, ROUND_HALF_UP to 2 dp; `UNIT_CONVERTED` when factor ≠ 1."""
        if qty is None or unit is None:
            return None
        try:
            base = (qty * unit.factor).quantize(_CENT, rounding=ROUND_HALF_UP)
        except InvalidOperation:  # past the context precision: unreadable, never raise
            self.status["qty"] = CellStatus.UNCLEAR
            return None
        if unit.factor != 1:
            self._log("qty_base", NormCode.UNIT_CONVERTED, _text(self._value("qty")), str(base))
        return base

    def text(self, name: str, *, upper: bool = False) -> str | None:
        """F-04.R8: lots trimmed and upper-cased; items trimmed, display only."""
        raw = _text(self._value(name))
        if raw is None:
            return None
        after = raw.strip().upper() if upper else raw.strip()
        if after != raw:
            self._log(name, NormCode.TEXT_TRIMMED, raw, after or None)
        return after or None

    def build(self, raw: dict[str, str | None], meta: RowMeta) -> CanonicalRow:
        row_date = self.row_date()
        supplier_code = self.supplier()
        item = self.text("item")
        lot_no = self.text("lot_no", upper=True)
        qty = self.qty()
        unit = self.unit()
        qty_base = self.qty_base(qty, unit)
        if self.status["qty"] is CellStatus.UNCLEAR:  # qty_base found it out of range
            qty = None
        return CanonicalRow(
            id=stable_id(self.out.run_id, self.row_id),
            run_id=self.out.run_id,
            source=self.source,
            source_row_id=self.row_id,
            row_kind=RowKind.RECEIPT,
            grn_no=self.grn_no,
            date=row_date,
            supplier_code=supplier_code,
            item=item,
            lot_no=lot_no,
            qty=qty,
            uom=unit.unit if unit else None,
            qty_base=qty_base,
            base_uom=unit.base if unit else None,
            cell_status=dict(self.status),
            raw=raw,
            meta=meta,
            duplicate_of=None,
            group_id=None,
            outcome=None,
        )


def _register_rows(out: _Out, page: RegisterPageInput, run_year: int) -> list[CanonicalRow]:
    """F-04.R1, R9: `REG-p{p}-r{rr}`; merged value only when `CLEAR`, status into `cell_status`;
    year from `month_written`, else from the run month (research R4)."""
    year = _header_year(page.month_written) or run_year
    rows = []
    for merged_row in sorted(page.rows, key=lambda r: r.row_index):
        row_id = register_row_id(page.page_no, merged_row.row_index)
        cells = {
            REGISTER_TO_CANONICAL[f]: _Cell(c.merged.value, c.merged.status)
            for f, c in merged_row.cells.items()
        }
        raw = {f.value: c.merged.value for f, c in merged_row.cells.items()}
        norm = _RowNormaliser(out, Source.REGISTER, row_id, cells, year)
        meta = RowMeta(
            page_no=page.page_no,
            row_index=merged_row.row_index,
            received_by=norm.received_by(),
        )
        rows.append(norm.build(raw, meta))
    return rows


def _entered_at(row: RawRow) -> datetime | None:
    """ERP `entered_at` (`YYYY-MM-DD HH:MM`), aware in Asia/Karachi (research R2); null when
    blank or unreadable (never raises; `raw` keeps the text)."""
    text = _stripped(row.values.get("entered_at"))
    if text is None:
        return None
    try:
        naive = datetime.strptime(text, "%Y-%m-%d %H:%M")  # noqa: DTZ007
    except ValueError:
        return None
    return naive.replace(tzinfo=KARACHI)


def _stripped(value: object) -> str | None:
    """Trimmed text of a meta value (research R2); blank → None."""
    text = (_text(value) or "").strip()
    return text or None


def _file_row(out: _Out, source: Source, row: RawRow, run_year: int) -> CanonicalRow:
    """One ERP or SHEET row: columns to canonical cells, raw text kept, source meta."""
    if source is Source.ERP:
        columns = _ERP_TO_CANONICAL
        meta = RowMeta(
            entered_by=_stripped(row.values.get("entered_by")), entered_at=_entered_at(row)
        )
    else:
        columns = _SHEET_TO_CANONICAL
        meta = RowMeta(remarks=_text(row.values.get("Remarks")))
    cells = {name: _Cell(row.values.get(col)) for col, name in columns.items()}
    raw = {k: _text(v) for k, v in row.values.items()}
    return _RowNormaliser(out, source, row.source_row_id, cells, run_year).build(raw, meta)


def normalise_all(
    run_id: UUID,
    month: str,
    register_pages: list[RegisterPageInput],
    erp: list[RawRow],
    sheet: list[RawRow],
    mappings: MappingTables,
    decisions: list[ReviewItem],
) -> NormaliseResult:
    """F-04 over the three sources (contracts/normalise.md N1 to N14).

    Rows come out REGISTER (page, row), ERP (line), SHEET (row) (N11), all `RECEIPT` (N12).
    """
    run_year = int(month[:4])
    overrides = {
        d.item_key: d.resolved_value
        for d in decisions
        if d.status is ReviewStatus.RESOLVED
        and d.resolved_value is not None
        and d.kind in (ReviewKind.CELL, ReviewKind.UNKNOWN_MAPPING, ReviewKind.UNMATCHED_ROW)
    }
    out = _Out(run_id=run_id, run_year=run_year, mappings=mappings, overrides=overrides)
    rows: list[CanonicalRow] = []
    for page in sorted(register_pages, key=lambda p: p.page_no):
        rows.extend(_register_rows(out, page, run_year))
    for source, file_rows in ((Source.ERP, erp), (Source.SHEET, sheet)):
        rows.extend(
            _file_row(out, source, r, run_year) for r in sorted(file_rows, key=lambda r: r.line_no)
        )
    return NormaliseResult(
        rows=rows, norm_log=out.norm_log, flags=out.flags, review_items=out.review_items
    )
