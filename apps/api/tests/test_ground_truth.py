"""US6 / FR-026, FR-027: hand-typed ground truth for both register pages (GE-06, GE-08, GE-09)."""

import json
from pathlib import Path
from typing import Any

import pytest

from app.domain.enums import CellStatus, RegisterField
from app.domain.models import Cell, GroundTruthPage

PAGES = (1, 2)
CELL_KEYS = {"value", "status", "struck", "candidates", "note"}

# (page, row_index, field) cells that are not plain CLEAR transcriptions.
NON_CLEAR = {(2, 5, "qty"), (2, 14, "qty")}


def _raw(ground_truth_dir: Path, page: int) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (ground_truth_dir / f"register_p{page}.json").read_text(encoding="utf-8")
    )
    return data


@pytest.fixture(scope="module")
def raw_pages(ground_truth_dir: Path) -> dict[int, dict[str, Any]]:
    return {p: _raw(ground_truth_dir, p) for p in PAGES}


@pytest.fixture(scope="module")
def pages(raw_pages: dict[int, dict[str, Any]]) -> dict[int, GroundTruthPage]:
    return {p: GroundTruthPage.model_validate(raw) for p, raw in raw_pages.items()}


def _cell(pages: dict[int, GroundTruthPage], page: int, row: int, field: str) -> Cell:
    (match,) = [r for r in pages[page].rows if r.row_index == row]
    return match.cells[RegisterField(field)]


def test_files_validate_as_ground_truth_page(
    raw_pages: dict[int, dict[str, Any]], pages: dict[int, GroundTruthPage]
) -> None:
    for p in PAGES:
        assert pages[p].source == "ground_truth"
        assert pages[p].page_no == p
        # The JSON schema requires every cell key explicitly (no reliance on model defaults).
        for row in raw_pages[p]["rows"]:
            assert set(row) == {"row_index", "cells"}
            assert set(row["cells"]) == {f.value for f in RegisterField}
            for cell in row["cells"].values():
                assert set(cell) == CELL_KEYS


def test_fourteen_rows_two_blank_slots_each(pages: dict[int, GroundTruthPage]) -> None:
    for p in PAGES:
        assert [r.row_index for r in pages[p].rows] == list(range(1, 15))
        assert pages[p].blank_row_slots == 2


def test_page_labels(pages: dict[int, GroundTruthPage]) -> None:
    for p in PAGES:
        assert pages[p].month_written == "Sep 2026"
        assert pages[p].page_no_written == str(p)


def test_224_cells_present(pages: dict[int, GroundTruthPage]) -> None:
    total = sum(len(r.cells) for p in PAGES for r in pages[p].rows)
    assert total == 224


def test_4419_qty_corrected_60_to_66(pages: dict[int, GroundTruthPage]) -> None:
    assert _cell(pages, 2, 5, "grn_no").value == "4419"
    qty = _cell(pages, 2, 5, "qty")
    assert qty.status is CellStatus.CORRECTED
    assert qty.value == "66"
    assert qty.struck == "60"
    assert qty.candidates == []


def test_4428_qty_illegible_null_no_candidates(pages: dict[int, GroundTruthPage]) -> None:
    assert _cell(pages, 2, 14, "grn_no").value == "4428"
    qty = _cell(pages, 2, 14, "qty")
    assert qty.status is CellStatus.ILLEGIBLE
    assert qty.value is None
    assert qty.struck is None
    assert qty.candidates == []
    assert qty.note is not None and "ink blot" in qty.note
    assert "250" not in qty.note


def test_h202_verbatim(pages: dict[int, GroundTruthPage]) -> None:
    for page, row in ((1, 8), (2, 5)):
        item = _cell(pages, page, row, "item")
        assert item.value == "H202 50%"
        assert item.status is CellStatus.CLEAR


def test_4425_unit_yds(pages: dict[int, GroundTruthPage]) -> None:
    assert _cell(pages, 2, 11, "grn_no").value == "4425"
    assert _cell(pages, 2, 11, "unit").value == "yds"
    assert _cell(pages, 2, 11, "qty").value == "1200"


def test_4412_lot_L2609_012(pages: dict[int, GroundTruthPage]) -> None:  # noqa: N802
    assert _cell(pages, 1, 12, "grn_no").value == "4412"
    assert _cell(pages, 1, 12, "lot_no").value == "L2609-012"


def test_all_other_cells_clear(pages: dict[int, GroundTruthPage]) -> None:
    for p in PAGES:
        for row in pages[p].rows:
            for field, cell in row.cells.items():
                if (p, row.row_index, field.value) in NON_CLEAR:
                    continue
                assert cell.status is CellStatus.CLEAR, (p, row.row_index, field)
                assert cell.value, (p, row.row_index, field)
                assert cell.value == cell.value.strip()
                assert cell.struck is None
                assert cell.candidates == []
                assert cell.note is None


def test_quantities_are_strings(raw_pages: dict[int, dict[str, Any]]) -> None:
    for p in PAGES:
        for row in raw_pages[p]["rows"]:
            qty = row["cells"]["qty"]
            assert qty["value"] is None or isinstance(qty["value"], str)
            assert qty["struck"] is None or isinstance(qty["struck"], str)
            if isinstance(qty["value"], str):
                assert qty["value"].isdigit()


def test_table_bounds_present_and_ordered(pages: dict[int, GroundTruthPage]) -> None:
    for p in PAGES:
        top, bottom = pages[p].table_top, pages[p].table_bottom
        assert top is not None and bottom is not None
        assert 0 < top < bottom < 1
        # Spec Reference Data: top ~0.145, bottom ~0.945-0.950 (tolerance 0.02).
        assert abs(top - 0.145) <= 0.02
        assert abs(bottom - 0.9475) <= 0.02


def _resolve(schema: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    ref = node.get("$ref")
    if ref is None:
        return node
    resolved: dict[str, Any] = schema["$defs"][ref.rsplit("/", 1)[-1]]
    return resolved


def test_json_schema_matches_model(ground_truth_dir: Path) -> None:
    contract = json.loads(
        (ground_truth_dir / "ground-truth.schema.json").read_text(encoding="utf-8")
    )
    model = GroundTruthPage.model_json_schema()

    # Page level: same keys, same required keys.
    assert set(contract["properties"]) == set(model["properties"])
    assert set(contract["required"]) == set(model["required"])
    assert contract["properties"]["source"]["const"] == model["properties"]["source"]["const"]

    # Row level.
    c_row = contract["$defs"]["row"]
    m_row = _resolve(model, model["properties"]["rows"]["items"])
    assert set(c_row["properties"]) == set(m_row["properties"])
    assert set(c_row["required"]) == set(m_row["required"])

    # Cells: the eight register fields (enum) are exactly the contract's cell keys.
    m_field_enum = _resolve(model, m_row["properties"]["cells"]["propertyNames"])["enum"]
    assert set(c_row["properties"]["cells"]["required"]) == set(m_field_enum)
    assert set(c_row["properties"]["cells"]["properties"]) == set(m_field_enum)

    # Cell level: same keys; the contract requires every key, the model defaults
    # struck/candidates/note, so the model's required set must be a subset.
    c_cell = contract["$defs"]["cell"]
    m_cell = _resolve(model, m_row["properties"]["cells"]["additionalProperties"])
    assert set(c_cell["properties"]) == set(m_cell["properties"])
    assert set(m_cell["required"]) <= set(c_cell["required"])
    assert set(c_cell["required"]) == set(c_cell["properties"])

    # Status enum.
    m_status = _resolve(model, m_cell["properties"]["status"])["enum"]
    assert c_cell["properties"]["status"]["enum"] == m_status
