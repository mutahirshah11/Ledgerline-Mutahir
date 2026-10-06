"""The pack's register pages through the real extract service (T027; F-03.AC1-AC4, AC6).

`pack_pages` (conftest) is parametrised: `cache` extracts from the committed real reads in
`supabase/seed/extractions/` (MemoryRepo loaded by `load_cached_extractions`, ReplayModelClient)
and skips until `scripts.seed_cache --capture` has run; `live` reads afresh with the model, is
marked `live` and skips without OPENAI_API_KEY (research R13). The assertion helpers below also
run, offline and always, on reads derived from the ground truth
(`test_F03_pack_assertions_hold_on_ground_truth_reads`), so their logic is exercised before any
real read exists.
"""

from fractions import Fraction
from pathlib import Path

import pytest

from app.domain.enums import CellStatus, RegisterField
from app.domain.models import Cell
from scripts.eval_extraction import evaluate, load_ground_truth
from tests.support.extracted import ground_truth_read
from tests.support.pack import PAGES
from tests.support.pack_pages import PackPages, cached_pack, write_cache_files

EXPECTED_REVIEW_KEYS = {"CELL:REG-p2-r05:qty", "CELL:REG-p2-r14:qty"}
"""The only review items the golden counts allow from extraction: 4419's correction and 4428's
ink blot (Section 8)."""


def merged(pages: PackPages, page_no: int, row_index: int, field: RegisterField) -> Cell:
    (row,) = [r for r in pages[page_no].page.rows if r.row_index == row_index]
    return row.cells[field].merged


# --- assertions (shared by the cache/live tests and the ground-truth check) ----------------


def check_ac1_ink_blot(pages: PackPages) -> None:
    assert merged(pages, 2, 14, RegisterField.GRN_NO).value == "4428"
    qty = merged(pages, 2, 14, RegisterField.QTY)
    assert qty.value is None
    if pages.source == "live":  # C3: a live read may call it UNCLEAR; never a guessed CLEAR
        assert qty.status is not CellStatus.CLEAR
    else:
        assert qty.status is CellStatus.ILLEGIBLE


def check_ac2_correction(pages: PackPages) -> None:
    assert merged(pages, 2, 5, RegisterField.GRN_NO).value == "4419"
    qty = merged(pages, 2, 5, RegisterField.QTY)
    assert qty.status is CellStatus.CORRECTED
    assert qty.struck == "60"
    assert qty.value == "66"


def check_ac3_verbatim(pages: PackPages) -> None:
    """Exact strings: no unit normalisation, lot format kept, "H202" not "H2O2"."""
    assert merged(pages, 2, 11, RegisterField.UNIT).value == "yds"
    assert merged(pages, 1, 12, RegisterField.LOT_NO).value == "L2609-012"
    assert merged(pages, 1, 8, RegisterField.ITEM).value == "H202 50%"


def check_ac4_rows(pages: PackPages) -> None:
    for p in PAGES:
        page = pages[p].page
        assert len(page.rows) == 14, f"page {p}"
        assert (page.row_count_a, page.row_count_b) == (14, 14), f"page {p}"
        assert (page.blank_row_slots_a, page.blank_row_slots_b) == (2, 2), f"page {p}"


def check_ac6_accuracy(pages: PackPages) -> None:
    report = evaluate(load_ground_truth(), pages.pairs)
    assert report.pages == list(PAGES)
    accuracy = report.merged.accuracy
    assert accuracy is not None and accuracy >= Fraction(98, 100), report.mismatches
    assert report.guessed == []


def check_review_items(pages: PackPages) -> None:
    keys = [item.item_key for p in PAGES for item in pages[p].review_items]
    assert sorted(keys) == sorted(EXPECTED_REVIEW_KEYS)
    assert all(pages[p].disagreements == 0 for p in PAGES)


# --- the pack, from the committed cache or live --------------------------------------------


def test_F03_AC1_ink_blot_4428_not_guessed(pack_pages: PackPages) -> None:
    check_ac1_ink_blot(pack_pages)


def test_F03_AC2_correction_4419(pack_pages: PackPages) -> None:
    check_ac2_correction(pack_pages)


def test_F03_AC3_verbatim_yds_and_lot(pack_pages: PackPages) -> None:
    check_ac3_verbatim(pack_pages)


def test_F03_AC4_fourteen_rows_two_blank(pack_pages: PackPages) -> None:
    check_ac4_rows(pack_pages)


def test_F03_AC6_accuracy_and_no_guess(pack_pages: PackPages) -> None:
    check_ac6_accuracy(pack_pages)


def test_F03_pack_no_reviewable_disagreements(pack_pages: PackPages) -> None:
    """Cache only: the committed reads queue exactly 4419's and 4428's qty, so the golden
    counts hold for the `cache` source (FR-035). A live read may differ run to run."""
    if pack_pages.source != "cache":
        pytest.skip("cache only: the golden counts are pinned to the committed reads")
    check_review_items(pack_pages)


# --- the same assertions on ground-truth reads (offline, always runs) ----------------------


def test_F03_pack_assertions_hold_on_ground_truth_reads(
    tmp_path: Path, pack_dir: Path, ground_truth_dir: Path
) -> None:
    """The ground truth written as cache files (both reads = the ground truth) and extracted
    by the `cache` path passes every pack assertion: the helpers check the right cells and
    the loader works. Synthetic reads live in `tmp_path` only, never in the committed cache
    (Clarification 4)."""
    reads = {p: (ground_truth_read(ground_truth_dir, p),) * 2 for p in PAGES}
    write_cache_files(tmp_path, pack_dir, reads)

    pages = cached_pack(tmp_path, pack_dir)

    assert pages.source == "cache"
    assert all(pages[p].cached and pages[p].calls_made == 0 for p in PAGES)
    check_ac1_ink_blot(pages)
    check_ac2_correction(pages)
    check_ac3_verbatim(pages)
    check_ac4_rows(pages)
    check_ac6_accuracy(pages)
    check_review_items(pages)


def test_F03_pack_assertions_catch_a_guess(
    tmp_path: Path, pack_dir: Path, ground_truth_dir: Path
) -> None:
    """A read that guesses 4428's qty fails AC1 and AC6 (the helpers are not vacuous)."""
    gt2 = ground_truth_read(ground_truth_dir, 2)
    rows = [
        r.model_copy(
            update={
                "cells": r.cells | {RegisterField.QTY: Cell(value="200", status=CellStatus.CLEAR)}
            }
        )
        if r.row_index == 14
        else r
        for r in gt2.rows
    ]
    guessed = gt2.model_copy(update={"rows": rows})
    reads = {1: (ground_truth_read(ground_truth_dir, 1),) * 2, 2: (guessed, guessed)}
    write_cache_files(tmp_path, pack_dir, reads)

    pages = cached_pack(tmp_path, pack_dir)

    with pytest.raises(AssertionError):
        check_ac1_ink_blot(pages)
    with pytest.raises(AssertionError):
        check_ac6_accuracy(pages)
