"""F-04 normalisation (contracts/normalise.md). Pure; the only I/O is reading the shipped
mapping files in `load_mappings`. No clock use."""

import csv
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import TypeVar

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
