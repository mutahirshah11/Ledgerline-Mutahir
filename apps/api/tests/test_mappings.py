"""F-04.R5, R6; research R5: shipped mapping files (`app/mappings/*.csv`)."""

from decimal import Decimal
from pathlib import Path

import pytest

from app.pipeline.normalise import MappingTables, UnitDef, load_mappings

ALL_CODES = {"S-101", "S-102", "S-103", "S-104", "S-105", "S-106", "S-107"}

ALIASES_TO_CODE = {
    "qasr pkg": "S-101",
    "qasr packaging": "S-101",
    "taraz poly": "S-102",
    "zarnab chem": "S-103",
    "zarnab chemicals": "S-103",
    "rehbar threads": "S-104",
    "rehbar thread": "S-104",
    "saahil pallets": "S-105",
    "nayab labels": "S-106",
    "shahtoot weaving": "S-107",
    "shahtoot": "S-107",
}

ALIASES_TO_UNIT = {
    "pcs": ("PCS", "PCS", Decimal("1")),
    "bags": ("BAG", "BAG", Decimal("1")),
    "bag": ("BAG", "BAG", Decimal("1")),
    "cones": ("CONE", "CONE", Decimal("1")),
    "cone": ("CONE", "CONE", Decimal("1")),
    "drums": ("DRUM", "DRUM", Decimal("1")),
    "drum": ("DRUM", "DRUM", Decimal("1")),
    "rolls": ("ROLL", "ROLL", Decimal("1")),
    "roll": ("ROLL", "ROLL", Decimal("1")),
    "ctns": ("CTN", "CTN", Decimal("1")),
    "ctn": ("CTN", "CTN", Decimal("1")),
    "bdl": ("BDL", "BDL", Decimal("1")),
    "m": ("M", "M", Decimal("1")),
    "yds": ("YD", "M", Decimal("0.9144")),
    "yd": ("YD", "M", Decimal("0.9144")),
}

SUPPLIERS_OK = "alias,code\nqasr pkg,S-101\n"
UNITS_OK = "alias,unit,base,factor\nm,M,M,1\nyd,YD,M,0.9144\n"


@pytest.fixture(scope="module")
def mappings() -> MappingTables:
    return load_mappings()


def _write(tmp_path: Path, suppliers: str = SUPPLIERS_OK, units: str = UNITS_OK) -> Path:
    (tmp_path / "supplier_map.csv").write_text(suppliers, encoding="utf-8")
    (tmp_path / "unit_map.csv").write_text(units, encoding="utf-8")
    return tmp_path


def test_shipped_files_load(mappings: MappingTables) -> None:
    assert mappings.suppliers
    assert mappings.units
    assert mappings.codes == ALL_CODES


@pytest.mark.parametrize("alias", sorted(ALIASES_TO_CODE))
def test_every_supplier_alias_resolves(mappings: MappingTables, alias: str) -> None:
    assert mappings.supplier_code(alias) == ALIASES_TO_CODE[alias]


@pytest.mark.parametrize("alias", sorted(ALIASES_TO_UNIT))
def test_every_unit_alias_resolves(mappings: MappingTables, alias: str) -> None:
    unit, base, factor = ALIASES_TO_UNIT[alias]
    assert mappings.unit_def(alias) == UnitDef(unit=unit, base=base, factor=factor)


@pytest.mark.parametrize("text", ["  Qasr\tPKG ", "qasr   pkg", "QASR PKG"])
def test_lookup_trims_collapses_and_casefolds(mappings: MappingTables, text: str) -> None:
    """Research R5 key: trim, collapse inner whitespace, casefold."""
    assert mappings.supplier_code(text) == "S-101"


def test_unit_lookup_uses_same_key(mappings: MappingTables) -> None:
    assert mappings.unit_def("  Yds ") == UnitDef(unit="YD", base="M", factor=Decimal("0.9144"))
    assert mappings.unit_def("boxes") is None


def test_lookup_does_not_fuzzy_match(mappings: MappingTables) -> None:
    assert mappings.supplier_code("qasr pckg") is None
    assert mappings.supplier_code("") is None


def test_codes_are_the_fixed_list(tmp_path: Path) -> None:
    """Research R5: valid ERP codes are S-101..S-107, even one with no alias in the file."""
    assert load_mappings(_write(tmp_path)).codes == ALL_CODES


def test_files_with_bom_load(tmp_path: Path) -> None:
    (tmp_path / "supplier_map.csv").write_text(SUPPLIERS_OK, encoding="utf-8-sig")
    (tmp_path / "unit_map.csv").write_text(UNITS_OK, encoding="utf-8-sig")
    tables = load_mappings(tmp_path)
    assert tables.supplier_code("qasr pkg") == "S-101"
    assert tables.unit_def("yd") is not None


@pytest.mark.parametrize(
    ("suppliers", "units", "match"),
    [
        ("alias,code\nqasr pkg,S-101\nqasr pkg,S-102\n", UNITS_OK, "duplicate alias"),
        ("alias,code\nqasr pkg,S-101\nQasr  PKG,S-101\n", UNITS_OK, "duplicate alias"),
        ("alias,code\nqasr pkg,S-999\n", UNITS_OK, "not one of S-101..S-107"),
        ("alias,supplier\nqasr pkg,S-101\n", UNITS_OK, "header must be alias,code"),
        ("alias,code\n  ,S-101\n", UNITS_OK, "blank alias"),
        ("alias,code\nqasr pkg,S-101,extra\n", UNITS_OK, "expected 2 columns"),
        (SUPPLIERS_OK, "alias,unit,base\nm,M,M\n", "header must be alias,unit,base,factor"),
        (SUPPLIERS_OK, "alias,unit,base,factor\nm,M,M,1\nyd,YD,M,0\n", "positive factor"),
        (SUPPLIERS_OK, "alias,unit,base,factor\nm,M,M,1\nyd,YD,M,-1\n", "positive factor"),
        (SUPPLIERS_OK, "alias,unit,base,factor\nm,M,M,1\nyd,YD,M,abc\n", "not a decimal"),
        (SUPPLIERS_OK, "alias,unit,base,factor\nm,M,M,1\nyd,YD,M,Infinity\n", "positive factor"),
        (SUPPLIERS_OK, "alias,unit,base,factor\nm,M,M,1\nyd,YD,M\n", "expected 4 columns"),
        (SUPPLIERS_OK, "alias,unit,base,factor\nyd,YD,M,0.9144\n", "not defined as a unit"),
        (SUPPLIERS_OK, UNITS_OK + "yds,YD,M,0.91\n", "aliases of YD disagree"),
        (SUPPLIERS_OK, UNITS_OK + "yds,YD,CTN,0.9144\nctn,CTN,CTN,1\n", "aliases of YD disagree"),
        (SUPPLIERS_OK, "alias,unit,base,factor\nm,M,M,2\n", "base unit M needs factor 1"),
        (SUPPLIERS_OK, UNITS_OK + "ft,FT,YD,0.3048\n", "not its own base"),
    ],
)
def test_bad_mapping_files_raise(tmp_path: Path, suppliers: str, units: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        load_mappings(_write(tmp_path, suppliers, units))
