"""Phase 2 extensions to 001-foundation (data-model.md §6, E1 to E7)."""

from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core import config
from app.core.config import Settings
from app.core.errors import (
    InvalidReadOutput,
    LedgerlineError,
    PageUnreadable,
    UpstreamFailed,
)
from app.domain.enums import RegisterField
from app.domain.ids import page_review_key, row_review_key
from app.domain.models import MergedCell, MergedPage, MergedRow, PageExtractionResult
from tests.factories import cell, make_review, sha


def _row(page_no: int = 2, row_index: int = 1) -> MergedRow:
    merged = MergedCell(merged=cell(), read_a=cell(), read_b=cell(), review_reason=None)
    return MergedRow(page_no=page_no, row_index=row_index, cells={f: merged for f in RegisterField})


def _page(**over: Any) -> MergedPage:
    base: dict[str, Any] = dict(
        page_no=2,
        page_sha256=sha(2),
        month_written="Sep 2026",
        page_no_written="2",
        table_top=0.21,
        table_bottom=0.93,
        blank_row_slots_a=1,
        blank_row_slots_b=1,
        row_count_a=15,
        row_count_b=15,
        rows=[_row(row_index=3), _row(row_index=1)],
    )
    return MergedPage(**(base | over))


# --- E1 MergedPage -----------------------------------------------------------------------


def test_F03_E1_merged_page_validates_and_sorts_rows() -> None:
    page = _page()
    assert [r.row_index for r in page.rows] == [1, 3]
    assert page.page_sha256 == sha(2)


def test_F03_E1_merged_page_allows_null_header_and_bounds() -> None:
    page = _page(month_written=None, page_no_written=None, table_top=None, table_bottom=None)
    assert page.table_top is None


@pytest.mark.parametrize(
    "over",
    [
        {"page_no": 0},
        {"page_sha256": "not-a-hash"},
        {"table_top": 1.5},
        {"table_bottom": -0.1},
        {"blank_row_slots_a": -1},
        {"blank_row_slots_b": 17},
        {"row_count_a": -1},
        {"row_count_b": 17},
        {"rows": [_row(row_index=1), _row(row_index=1)]},
        {"rows": [_row(page_no=3)]},
        {"extra": 1},
    ],
)
def test_F03_E1_merged_page_rejects_bad_values(over: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _page(**over)


def test_F03_E1_merged_page_accepts_unordered_bounds() -> None:
    """Merged bounds are averaged per field (data-model §5); each PageRead orders its own."""
    page = _page(table_top=0.9, table_bottom=0.2)
    assert (page.table_top, page.table_bottom) == (0.9, 0.2)
    assert _page(table_top=0.5, table_bottom=0.5).table_top == 0.5


def test_F03_E1_merged_page_is_frozen() -> None:
    page = _page()
    with pytest.raises(ValidationError):
        page.page_no = 3  # type: ignore[misc]


# --- E2 PageExtractionResult -------------------------------------------------------------


def _result(**over: Any) -> PageExtractionResult:
    base: dict[str, Any] = dict(
        page_no=2,
        cached=False,
        page=_page(),
        review_items=[make_review(uuid4(), "CELL:REG-p2-r01:qty")],
        review_cells=1,
        disagreements=0,
        calls_made=2,
    )
    return PageExtractionResult(**(base | over))


def test_F03_E2_page_extraction_result_validates() -> None:
    result = _result()
    assert result.page.rows[0].row_index == 1
    assert len(result.review_items) == 1
    assert _result(cached=True, calls_made=0, review_items=[], review_cells=0).cached is True


@pytest.mark.parametrize(
    "over",
    [
        {"page_no": 3},
        {"review_cells": -1},
        {"disagreements": -1},
        {"calls_made": -1},
        {"review_cells": 0},
        {"review_cells": 2},
        {"cached": True},
        {"calls_made": 0},
        {"extra": 1},
    ],
)
def test_F03_E2_page_extraction_result_rejects_bad_values(over: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _result(**over)


def test_F03_E2_page_extraction_result_is_frozen() -> None:
    result = _result()
    with pytest.raises(ValidationError):
        result.cached = True  # type: ignore[misc]


# --- E3 review keys ----------------------------------------------------------------------


def test_F03_E3_row_and_page_review_keys() -> None:
    assert row_review_key(2, 15) == "CELL:REG-p2-r15:row"
    assert row_review_key(1, 1) == "CELL:REG-p1-r01:row"
    assert page_review_key(2) == "CELL:REG-p2:page"


@pytest.mark.parametrize("args", [(0, 1), (1, 0), (1, 17), (True, 1), (1, "2")])
def test_F03_E3_row_review_key_rejects_bad_input(args: tuple[Any, Any]) -> None:
    with pytest.raises(ValueError):
        row_review_key(*args)


@pytest.mark.parametrize("page_no", [0, -1, True, "2"])
def test_F03_E3_page_review_key_rejects_bad_input(page_no: Any) -> None:
    with pytest.raises(ValueError):
        page_review_key(page_no)


# --- E4, E5, E7 errors -------------------------------------------------------------------


def test_F03_E4_page_unreadable_fields_and_message() -> None:
    err = PageUnreadable(2, 70, 112)
    assert (err.page_no, err.illegible, err.total) == (2, 70, 112)
    assert str(err) == "Page 2 could not be read, retake the photo"
    assert isinstance(err, LedgerlineError)


def test_F03_E5_invalid_read_output_carries_detail() -> None:
    err = InvalidReadOutput("rows is not a list")
    assert err.detail == "rows is not a list"
    assert "rows is not a list" in str(err)
    assert isinstance(err, LedgerlineError)


def test_F03_E7_upstream_failed_carries_stage_and_page() -> None:
    err = UpstreamFailed(service="openai", detail="x", stage="extract", page_no=2)
    assert (err.service, err.detail, err.stage, err.page_no) == ("openai", "x", "extract", 2)


def test_F03_E7_upstream_failed_two_argument_form_still_works() -> None:
    err = UpstreamFailed("supabase", "timeout")
    assert (err.service, err.detail, err.stage, err.page_no) == ("supabase", "timeout", None, None)
    assert str(err) == "supabase failed: timeout"


# --- E6 settings and constants -----------------------------------------------------------


def test_F03_E6_extract_settings_defaults() -> None:
    settings = Settings()
    assert settings.EXTRACT_MODEL == "gpt-5.6-luna"
    assert settings.PROMPT_VERSION == "v3"


def test_F03_E6_extract_retry_constants() -> None:
    assert config.EXTRACT_TIMEOUT_S == 60
    assert config.EXTRACT_ATTEMPTS == 3
    assert config.EXTRACT_WAITS_S == (2, 8)
