"""F-04.R5, R6; research R5: shipped mapping files (`app/mappings/*.csv`)."""

from decimal import Decimal
from pathlib import Path

import pytest

from app.pipeline.normalise import load_mappings

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


@pytest.fixture(scope="module")
def mappings():  # type: ignore[no-untyped-def]
    return load_mappings()


def test_shipped_files_load(mappings) -> None:  # type: ignore[no-untyped-def]
    assert mappings.suppliers
    assert mappings.units
    assert mappings.codes == {"S-101", "S-102", "S-103", "S-104", "S-105", "S-106", "S-107"}


@pytest.mark.parametrize("alias", sorted(ALIASES_TO_CODE))
def test_every_supplier_alias_resolves(mappings, alias: str) -> None:  # type: ignore[no-untyped-def]
    assert mappings.suppliers[alias] == ALIASES_TO_CODE[alias]


@pytest.mark.parametrize("alias", sorted(ALIASES_TO_UNIT))
def test_every_unit_alias_resolves(mappings, alias: str) -> None:  # type: ignore[no-untyped-def]
    unit, base, factor = ALIASES_TO_UNIT[alias]
    unit_def = mappings.units[alias]
    assert (unit_def.unit, unit_def.base, unit_def.factor) == (unit, base, factor)


def test_lookup_trims_and_casefolds(mappings) -> None:  # type: ignore[no-untyped-def]
    assert mappings.suppliers["  Qasr Pkg  ".strip().casefold()] == "S-101"
    assert mappings.units["  Yds ".strip().casefold()].unit == "YD"


def test_lookup_does_not_fuzzy_match(mappings) -> None:  # type: ignore[no-untyped-def]
    assert "qasr pckg" not in mappings.suppliers


def test_duplicate_alias_raises(tmp_path: Path) -> None:
    (tmp_path / "supplier_map.csv").write_text("alias,code\nqasr pkg,S-101\nqasr pkg,S-102\n")
    (tmp_path / "unit_map.csv").write_text("alias,unit,base,factor\npcs,PCS,PCS,1\n")
    with pytest.raises(ValueError):
        load_mappings(tmp_path)


def test_unknown_code_raises(tmp_path: Path) -> None:
    (tmp_path / "supplier_map.csv").write_text("alias,code\nqasr pkg,S-999\n")
    (tmp_path / "unit_map.csv").write_text("alias,unit,base,factor\npcs,PCS,PCS,1\n")
    with pytest.raises(ValueError):
        load_mappings(tmp_path)


def test_bad_factor_raises(tmp_path: Path) -> None:
    (tmp_path / "supplier_map.csv").write_text("alias,code\nqasr pkg,S-101\n")
    (tmp_path / "unit_map.csv").write_text("alias,unit,base,factor\nyd,YD,M,0\n")
    with pytest.raises(ValueError):
        load_mappings(tmp_path)


def test_unit_base_not_defined_raises(tmp_path: Path) -> None:
    (tmp_path / "supplier_map.csv").write_text("alias,code\nqasr pkg,S-101\n")
    (tmp_path / "unit_map.csv").write_text("alias,unit,base,factor\nyd,YD,M,0.9144\n")
    with pytest.raises(ValueError):
        load_mappings(tmp_path)
