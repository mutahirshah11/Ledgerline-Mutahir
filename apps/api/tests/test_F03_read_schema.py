"""F-03.R4: the strict read schema and `parse_read` validation (data-model §1, FM-05)."""

import json
from pathlib import Path
from typing import Any

import pytest

from app.core.errors import InvalidReadOutput
from app.domain.enums import CellStatus, RegisterField
from app.domain.models import Cell, PageRead
from app.pipeline.extract import load_read_schema, parse_read

API_DIR = Path(__file__).resolve().parents[1]
CONTRACT = API_DIR.parents[1] / "specs" / "003-extraction" / "contracts" / "read-schema.json"
GT_DIR = API_DIR / "tests" / "ground_truth"
FORBIDDEN_KEYWORDS = {"if", "then", "else", "allOf", "pattern", "minimum", "maximum"}


def _objects(node: Any) -> list[dict[str, Any]]:
    """Every JSON-schema object definition (type object) in the tree."""
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if node.get("type") == "object":
            found.append(node)
        for value in node.values():
            found.extend(_objects(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_objects(value))
    return found


def _keys(node: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key != "properties":  # property names are data, not keywords
                keys.add(key)
            keys |= _keys(value)
    elif isinstance(node, list):
        for value in node:
            keys |= _keys(value)
    return keys


def _gt_payload(page: int) -> dict[str, Any]:
    data = json.loads((GT_DIR / f"register_p{page}.json").read_text(encoding="utf-8"))
    del data["page_no"], data["source"]
    return dict(data)


def _cell(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "value": "1",
        "status": "CLEAR",
        "struck": None,
        "candidates": [],
        "note": None,
    }
    return base | over


def _payload() -> dict[str, Any]:
    return {
        "month_written": "Sep 2026",
        "page_no_written": "1",
        "table_top": 0.1,
        "table_bottom": 0.9,
        "blank_row_slots": 14,
        "rows": [
            {"row_index": i, "cells": {f.value: _cell() for f in RegisterField}} for i in (1, 2)
        ],
    }


@pytest.mark.skipif(not CONTRACT.exists(), reason="specs/ is local (git-ignored)")
def test_F03_R4_schema_file_matches_contract() -> None:
    assert load_read_schema("v1") == json.loads(CONTRACT.read_text(encoding="utf-8"))


def test_F03_R4_schema_is_strict_safe() -> None:
    schema = load_read_schema("v1")
    objects = _objects(schema)
    assert len(objects) >= 4  # page, row, cells, cell
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])
    assert not (_keys(schema) & FORBIDDEN_KEYWORDS)


def test_F03_R4_schema_matches_pagerread() -> None:
    schema = load_read_schema("v1")
    assert set(schema["properties"]) == set(PageRead.model_fields)
    row = schema["properties"]["rows"]["items"]
    assert set(row["properties"]) == {"row_index", "cells"}
    assert row["properties"]["cells"]["required"] == [f.value for f in RegisterField]
    cell = schema["$defs"]["cell"]
    assert set(cell["properties"]) == set(Cell.model_fields)
    assert cell["properties"]["status"]["enum"] == [s.value for s in CellStatus]


@pytest.mark.parametrize("page", [1, 2])
def test_F03_R4_parse_accepts_ground_truth_pages(page: int) -> None:
    read = parse_read(_gt_payload(page))
    assert isinstance(read, PageRead)
    assert len(read.rows) == 14
    assert [r.row_index for r in read.rows] == sorted(r.row_index for r in read.rows)


def test_F03_R4_parse_sorts_rows() -> None:
    payload = _payload()
    payload["rows"].reverse()
    assert [r.row_index for r in parse_read(payload).rows] == [1, 2]


def _with(p: dict[str, Any], change: Any) -> dict[str, Any]:
    change(p)
    return p


def _cell_case(**over: Any) -> Any:
    return lambda p: _with(p, lambda q: q["rows"][0]["cells"].update(qty=_cell(**over)))


INVALID: dict[str, Any] = {
    "row_index_0": lambda p: _with(p, lambda q: q["rows"][0].update(row_index=0)),
    "row_index_17": lambda p: _with(p, lambda q: q["rows"][0].update(row_index=17)),
    "duplicate_row_index": lambda p: _with(p, lambda q: q["rows"][1].update(row_index=1)),
    "missing_cell_key": lambda p: _with(p, lambda q: q["rows"][0]["cells"].pop("qty")),
    "corrected_without_struck": _cell_case(value="66", status="CORRECTED"),
    "corrected_blank_value": _cell_case(value="  ", status="CORRECTED", struck="60"),
    "corrected_blank_struck": _cell_case(value="66", status="CORRECTED", struck=" "),
    "corrected_empty_value": _cell_case(value="", status="CORRECTED", struck="60"),
    "clear_with_candidates": _cell_case(candidates=["1", "7"]),
    "unclear_without_candidates": _cell_case(value=None, status="UNCLEAR"),
    "illegible_with_value": _cell_case(value="2", status="ILLEGIBLE"),
    "top_not_above_bottom": lambda p: p | {"table_top": 0.9, "table_bottom": 0.9},
    "blank_row_slots_17": lambda p: p | {"blank_row_slots": 17},
    # PR-11 / F-07.R3: rows plus blank_row_slots must equal the 16 printed slots
    "slots_total_17": lambda p: p | {"blank_row_slots": 15},
    "slots_total_15": lambda p: p | {"blank_row_slots": 13},
    "row_dropped_slots_kept": lambda p: _with(p, lambda q: q["rows"].pop()),
    "row_index_string": lambda p: _with(p, lambda q: q["rows"][0].update(row_index="3")),
    "blank_row_slots_bool": lambda p: p | {"blank_row_slots": True},
    "blank_row_slots_string": lambda p: p | {"blank_row_slots": "14"},
    "table_top_string": lambda p: p | {"table_top": "0.1"},
    "value_not_a_string": _cell_case(value=1),
    "unknown_key": lambda p: p | {"extra": 1},
    "rows_not_a_list": lambda p: p | {"rows": "not a list"},
    "non_json_text": lambda p: '{"rows": [',
    "json_array": lambda p: "[1, 2]",
    "none": lambda p: None,
}


@pytest.mark.parametrize("case", sorted(INVALID))
def test_F03_R4_parse_rejects_invalid(case: str) -> None:
    parse_read(_payload())  # the unbroken payload is valid
    broken = INVALID[case](_payload())
    with pytest.raises(InvalidReadOutput):
        parse_read(broken)


def test_F03_R4_parse_accepts_enum_strings_and_json_text() -> None:
    read = parse_read(json.dumps(_payload()))
    cell = read.rows[0].cells[RegisterField.QTY]
    assert cell.status is CellStatus.CLEAR
    corrected = _payload()
    corrected["rows"][0]["cells"]["qty"] = _cell(value="66", status="CORRECTED", struck="60")
    assert parse_read(corrected).rows[0].cells[RegisterField.QTY].status is CellStatus.CORRECTED
