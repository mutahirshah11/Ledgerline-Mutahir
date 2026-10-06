"""F-03 scripts: extraction eval (T028/T030, research R11, FR-032, FR-033) and seed cache.

All tests are offline and synthetic: tiny ground-truth pages and reads built in code, no pack
images, no model calls.
"""

import json
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

from app.domain.enums import CellStatus, RegisterField
from app.domain.models import Cell, GroundTruthPage, PageRead, RowRead
from scripts.eval_extraction import (
    CacheEmpty,
    ReadPair,
    cost_usd,
    evaluate,
    exit_code,
    gate_passed,
    load_cached_reads,
    load_ground_truth,
    main,
    parse_pages,
)

SHA = "a" * 64

# =========================================================================================
# Eval (scripts/eval_extraction.py) — T028 eval tests
# =========================================================================================


def _clear(value: str | None) -> Cell:
    return Cell(value=value, status=CellStatus.CLEAR)


def _row(index: int, **overrides: Cell) -> RowRead:
    """A row whose 8 cells are CLEAR "<field>-<index>" unless overridden."""
    cells = {f: overrides.get(f.value, _clear(f"{f.value}-{index}")) for f in RegisterField}
    return RowRead(row_index=index, cells=cells)


def _read(*rows: RowRead) -> PageRead:
    return PageRead(
        month_written="Sep 2026",
        page_no_written="1",
        table_top=0.1,
        table_bottom=0.9,
        blank_row_slots=0,
        rows=list(rows),
    )


def _gt(page_no: int, *rows: RowRead) -> GroundTruthPage:
    read = _read(*rows)
    return GroundTruthPage(**read.model_dump(), page_no=page_no, source="ground_truth")


def _pair(page_no: int, read_a: PageRead, read_b: PageRead | None = None) -> ReadPair:
    return ReadPair(
        page_no=page_no,
        page_sha256=SHA,
        read_a=read_a,
        read_b=read_a if read_b is None else read_b,
        input_tokens=0,
        output_tokens=0,
    )


def test_F03_eval_accuracy_formula() -> None:
    # GT: 2 rows = 16 CLEAR cells. Read: one wrong value, one UNCLEAR, one matching only after
    # trim + case-fold, one CLEAR blank that GT also has blank.
    gt = _gt(1, _row(1, unit=_clear(None)), _row(2))
    read = _read(
        _row(1, unit=_clear("  "), supplier=_clear("  SUPPLIER-1 ")),
        _row(
            2,
            qty=_clear("999"),
            lot_no=Cell(value=None, status=CellStatus.UNCLEAR, candidates=["lot_no-2"]),
        ),
    )
    report = evaluate({1: gt}, [_pair(1, read)])
    # accuracy = GT CLEAR cells read CLEAR with the "same" value / all GT CLEAR cells
    assert (report.merged.correct, report.merged.total) == (14, 16)
    assert report.merged.accuracy == Fraction(14, 16)
    assert {(m.row_index, m.field) for m in report.mismatches} == {(2, "qty"), (2, "lot_no")}
    # per-read accuracy uses the same rule on each read alone
    assert (report.read_a.correct, report.read_a.total) == (14, 16)
    # a GT row missing from both reads counts every CLEAR cell as wrong
    missing = evaluate({1: gt}, [_pair(1, _read(_row(1, unit=_clear(None))))])
    assert (missing.merged.correct, missing.merged.total) == (8, 16)


def test_F03_eval_accuracy_counts_reads_disagreement_as_wrong() -> None:
    gt = _gt(1, _row(1))
    report = evaluate({1: gt}, [_pair(1, _read(_row(1)), _read(_row(1, qty=_clear("7"))))])
    assert (report.merged.correct, report.merged.total) == (7, 8)
    assert (report.read_a.correct, report.read_b.correct) == (8, 7)
    assert [(d.row_index, d.field) for d in report.disagreements] == [(1, "qty")]


def test_F03_eval_guessed_count() -> None:
    illegible = Cell(value=None, status=CellStatus.ILLEGIBLE)
    gt = _gt(2, _row(14, qty=illegible))
    guessed = evaluate({2: gt}, [_pair(2, _read(_row(14, qty=_clear("40"))))])
    assert [(g.page_no, g.row_index, g.field) for g in guessed.guessed] == [(2, 14, "qty")]
    # ILLEGIBLE cells are not in the accuracy denominator
    assert guessed.merged.total == 7
    honest = evaluate({2: gt}, [_pair(2, _read(_row(14, qty=illegible)))])
    assert honest.guessed == []
    blank = evaluate({2: gt}, [_pair(2, _read(_row(14, qty=_clear(None))))])
    assert blank.guessed == []  # CLEAR with no value is not a guess (R11: non-null value)
    # one read guessing is caught by the merge (UNCLEAR), not counted as guessed
    one_read = _read(_row(14, qty=_clear("40")))
    split = evaluate({2: gt}, [_pair(2, _read(_row(14, qty=illegible)), one_read)])
    assert split.guessed == []


def test_F03_eval_corrected_scoring() -> None:
    corrected = Cell(value="66", status=CellStatus.CORRECTED, struck="60")
    gt = _gt(2, _row(5, qty=corrected))
    good = evaluate({2: gt}, [_pair(2, _read(_row(5, qty=corrected)))])
    assert (good.corrected.correct, good.corrected.total) == (1, 1)
    assert good.merged.total == 7  # CORRECTED cells are scored apart from the CLEAR accuracy
    assert good.mismatches == []
    for wrong in (
        _clear("66"),
        Cell(value="66", status=CellStatus.CORRECTED, struck="80"),
        Cell(value="68", status=CellStatus.CORRECTED, struck="60"),
    ):
        bad = evaluate({2: gt}, [_pair(2, _read(_row(5, qty=wrong)))])
        assert (bad.corrected.correct, bad.corrected.total) == (0, 1)
        assert [(m.row_index, m.field, m.expected_status) for m in bad.mismatches] == [
            (5, "qty", "CORRECTED")
        ]


def test_F03_eval_exit_codes() -> None:
    # the gate compares exact fractions: correct / total >= 98 / 100 and guessed == 0
    assert gate_passed(50, 50, guessed=0)
    assert gate_passed(49, 50, guessed=0)  # exactly 0.98 passes
    assert not gate_passed(391, 400, guessed=0)  # 0.9775
    assert not gate_passed(50, 50, guessed=1)
    assert not gate_passed(0, 0, guessed=0)  # nothing scored is not a pass

    illegible = Cell(value=None, status=CellStatus.ILLEGIBLE)
    gt = {1: _gt(1, *(_row(i) for i in range(1, 8)), _row(8, qty=illegible))}  # 63 CLEAR
    perfect = [_row(i) for i in range(1, 8)] + [_row(8, qty=illegible)]
    assert exit_code(evaluate(gt, [_pair(1, _read(*perfect))])) == 0
    one_wrong = [_row(1, qty=_clear("0"))] + perfect[1:]  # 62/63 = 0.984
    assert exit_code(evaluate(gt, [_pair(1, _read(*one_wrong))])) == 0
    two_wrong = [_row(1, qty=_clear("0"), unit=_clear("0"))] + perfect[1:]  # 61/63 = 0.968
    assert exit_code(evaluate(gt, [_pair(1, _read(*two_wrong))])) == 1
    guess = perfect[:7] + [_row(8, qty=_clear("5"))]
    assert exit_code(evaluate(gt, [_pair(1, _read(*guess))])) == 1


def test_F03_eval_cli_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    gt = {1: _gt(1, _row(1)), 2: _gt(2, _row(1))}

    def good(pages: tuple[int, ...], live: bool) -> list[ReadPair]:
        return [_pair(p, _read(_row(1))) for p in pages]

    def bad(pages: tuple[int, ...], live: bool) -> list[ReadPair]:
        return [_pair(p, _read(_row(1, qty=_clear("0")))) for p in pages]

    def empty(pages: tuple[int, ...], live: bool) -> list[ReadPair]:
        raise CacheEmpty("no cached reads in supabase/seed/extractions")

    assert main([], loader=good, ground_truth=gt) == 0
    assert main([], loader=bad, ground_truth=gt) == 1
    capsys.readouterr()
    assert main([], loader=empty, ground_truth=gt) == 2
    assert "seed_cache --capture" in capsys.readouterr().err


def test_F03_eval_cost_formula() -> None:
    # cost = (input tokens x price_in + output tokens x price_out) / 1e6, exact Decimal
    assert cost_usd(1_000_000, 500_000, Decimal("1.25"), Decimal("10")) == Decimal("6.25")
    assert cost_usd(12_345, 678, Decimal("2"), Decimal("8")) == Decimal("0.030114")
    assert cost_usd(10, 10, None, Decimal("1")) is None  # no price, no cost
    gt = {1: _gt(1, _row(1))}
    pair = ReadPair(1, SHA, _read(_row(1)), _read(_row(1)), 3000, 1000)
    report = evaluate(gt, [pair], price_in=Decimal("1"), price_out=Decimal("4"))
    assert (report.input_tokens, report.output_tokens) == (3000, 1000)
    assert report.cost_usd == Decimal("0.007")


def test_F03_eval_pages_option(capsys: pytest.CaptureFixture[str]) -> None:
    assert parse_pages("1,2") == (1, 2)
    assert parse_pages(" 2 ") == (2,)
    assert parse_pages("2,1,2") == (1, 2)
    for bad in ("3", "", "a", "0"):
        with pytest.raises(ValueError):
            parse_pages(bad)

    seen: list[tuple[int, ...]] = []
    gt = {1: _gt(1, _row(1)), 2: _gt(2, _row(1))}

    def loader(pages: tuple[int, ...], live: bool) -> list[ReadPair]:
        seen.append(pages)
        return [_pair(p, _read(_row(1))) for p in pages]

    assert main(["--pages", "2", "--json"], loader=loader, ground_truth=gt) == 0
    assert seen == [(2,)]
    out = json.loads(capsys.readouterr().out)
    assert out["pages"] == [2]
    assert out["merged"]["total"] == 8
    assert out["passed"] is True


def test_F03_eval_load_cached_reads_empty_dir(tmp_path: Path) -> None:
    with pytest.raises(CacheEmpty):
        load_cached_reads((1, 2), cache_dir=tmp_path, model="m", prompt_version="v1")


def test_F03_eval_ground_truth_scores_itself_perfectly() -> None:
    gt = load_ground_truth()
    pairs = [_pair(p, gt[p]) for p in (1, 2)]
    report = evaluate(gt, pairs)
    assert (report.merged.correct, report.merged.total) == (222, 222)
    assert (report.corrected.correct, report.corrected.total) == (1, 1)
    assert report.guessed == [] and report.mismatches == [] and report.disagreements == []
    assert exit_code(report) == 0
