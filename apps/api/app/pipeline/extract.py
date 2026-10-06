"""F-03 extraction service (contracts/extract-service.md). Pure functions make no I/O except
reading the shipped prompt and schema files."""

import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.core.errors import InvalidReadOutput
from app.domain.enums import CellStatus, ReadVariant, RegisterField, ReviewReason
from app.domain.models import Cell, MergedCell, MergedPage, MergedRow, PageRead

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def load_prompt(variant: ReadVariant, version: str) -> str:
    """Return the instructions for one read variant at one prompt version (F-03.R2).

    Files are named `read_{variant}_{version}.md`; a missing file raises FileNotFoundError.
    """
    path = PROMPTS_DIR / f"read_{variant.value.lower()}_{version}.md"
    return path.read_text(encoding="utf-8")


def load_read_schema(version: str) -> dict[str, Any]:
    """Return the strict read schema shipped for a prompt version (F-03.R4).

    Files are named `read_schema_{version}.json`; a missing file raises FileNotFoundError.
    """
    path = PROMPTS_DIR / f"read_schema_{version}.json"
    schema: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return schema


def parse_read(payload: object) -> PageRead:
    """Validate one model answer as a `PageRead` (data-model §1, FM-05).

    Accepts the parsed JSON object (or its JSON text). Any failure, including text that is not
    a JSON object, raises `InvalidReadOutput`; rows come back sorted by `row_index`.
    """
    if isinstance(payload, str | bytes):
        try:
            payload = json.loads(payload)
        except ValueError as exc:
            raise InvalidReadOutput(f"not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise InvalidReadOutput(f"expected a JSON object, got {type(payload).__name__}")
    try:
        return PageRead.model_validate(payload)
    except ValidationError as exc:
        raise InvalidReadOutput(str(exc)) from exc


# --- merge (data-model §3, FR-015 to FR-021) ---------------------------------------------


def _same_key(value: str | None) -> str | None:
    """FR-015 comparison key: trimmed and case-folded; a blank string counts as null."""
    if value is None or not value.strip():
        return None
    return value.strip().casefold()


def _trimmed(value: str | None) -> str | None:
    return None if value is None or not value.strip() else value.strip()


def _union(*cells: Cell) -> list[str]:
    """FR-017: values (current value for CORRECTED) then candidates, read A first, trimmed,
    de-duplicated by the "same" rule."""
    seen: set[str] = set()
    out: list[str] = []
    for cell in cells:
        for raw in (cell.value, *cell.candidates):
            text, key = _trimmed(raw), _same_key(raw)
            if text is not None and key is not None and key not in seen:
                seen.add(key)
                out.append(text)
    return out


def _join_notes(*notes: str | None) -> str | None:
    """Non-blank notes joined with " | ", skipping ones the "same" rule says repeat."""
    seen: set[str] = set()
    out: list[str] = []
    for note in notes:
        text, key = _trimmed(note), _same_key(note)
        if text is not None and key is not None and key not in seen:
            seen.add(key)
            out.append(text)
    return " | ".join(out) if out else None


def _doubt(candidates: list[str], note: str | None) -> Cell:
    """A merged cell with no value: UNCLEAR with candidates, or ILLEGIBLE when none."""
    if candidates:
        return Cell(value=None, status=CellStatus.UNCLEAR, candidates=candidates, note=note)
    return Cell(value=None, status=CellStatus.ILLEGIBLE, note=note)


def _single_read_cell(cell: Cell) -> Cell:
    """FR-019 / Clarification 2: a cell only one read saw is never trusted."""
    value = _trimmed(cell.value)
    return _doubt([value] if value is not None else [], _trimmed(cell.note))


def merge_cells(a: Cell | None, b: Cell | None) -> MergedCell:
    """Merge one cell pair by the precedence table (data-model §3), first match wins.

    `None` stands for a row the other read alone saw (FR-019): the cell becomes UNCLEAR with
    the read's value as the only candidate, or ILLEGIBLE, and carries no review reason.
    """
    if a is None and b is None:
        raise ValueError("at least one read cell is required")
    if a is None or b is None:
        only = b if a is None else a
        assert only is not None
        return MergedCell(merged=_single_read_cell(only), read_a=a, read_b=b, review_reason=None)

    CLEAR, UNCLEAR = CellStatus.CLEAR, CellStatus.UNCLEAR
    CORRECTED, ILLEGIBLE = CellStatus.CORRECTED, CellStatus.ILLEGIBLE
    statuses = {a.status, b.status}
    note = _join_notes(a.note, b.note)
    reason: ReviewReason | None
    if ILLEGIBLE in statuses:  # 1
        merged = Cell(value=None, status=ILLEGIBLE, note=note)
        reason = ReviewReason.ILLEGIBLE
    elif (
        statuses == {CORRECTED}
        and _same_key(a.value) == _same_key(b.value)
        and _same_key(a.struck) == _same_key(b.struck)
    ):  # 2
        merged = Cell(
            value=_trimmed(a.value), status=CORRECTED, struck=_trimmed(a.struck), note=note
        )
        reason = ReviewReason.CORRECTED
    elif CORRECTED in statuses:  # 3
        merged = _doubt(_union(a, b), note)
        reason = ReviewReason.READS_DISAGREE
    elif UNCLEAR in statuses:  # 4
        merged = _doubt(_union(a, b), note)
        reason = ReviewReason.UNCLEAR
    elif _same_key(a.value) == _same_key(b.value):  # 5: both CLEAR
        merged = Cell(value=_trimmed(a.value), status=CLEAR, note=note)
        reason = None
    else:  # 6: both CLEAR, different
        blank = [
            v for v, c in ((ReadVariant.A, a), (ReadVariant.B, b)) if _same_key(c.value) is None
        ]
        if blank:
            note = _join_notes(note, f"read {blank[0].value} saw the cell blank")
        merged = _doubt(_union(a, b), note)
        reason = ReviewReason.READS_DISAGREE
    return MergedCell(merged=merged, read_a=a, read_b=b, review_reason=reason)


def _mean_edge(a: float | None, b: float | None) -> float | None:
    """FR-021: mean of both edges rounded half-up to 3 dp, else the one given, else None."""
    given = [Decimal(str(v)) for v in (a, b) if v is not None]
    if not given:
        return None
    mean = sum(given, Decimal(0)) / len(given)
    return float(mean.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))


def merge_reads(
    read_a: PageRead, read_b: PageRead, *, page_no: int, page_sha256: str
) -> MergedPage:
    """Align both reads by row index (F-03.R8) and merge every cell (FR-016 to FR-021).

    Rows are sorted by row index and cells keep the fixed field order (FR-020). Month and page
    number as written are read A's, or read B's when A has none.
    """
    rows_a = {r.row_index: r for r in read_a.rows}
    rows_b = {r.row_index: r for r in read_b.rows}
    rows: list[MergedRow] = []
    for index in sorted(rows_a.keys() | rows_b.keys()):
        row_a, row_b = rows_a.get(index), rows_b.get(index)
        cells = {
            field: merge_cells(
                None if row_a is None else row_a.cells[field],
                None if row_b is None else row_b.cells[field],
            )
            for field in RegisterField
        }
        rows.append(MergedRow(page_no=page_no, row_index=index, cells=cells))
    return MergedPage(
        page_no=page_no,
        page_sha256=page_sha256,
        month_written=read_a.month_written
        if read_a.month_written is not None
        else read_b.month_written,
        page_no_written=read_a.page_no_written
        if read_a.page_no_written is not None
        else read_b.page_no_written,
        table_top=_mean_edge(read_a.table_top, read_b.table_top),
        table_bottom=_mean_edge(read_a.table_bottom, read_b.table_bottom),
        blank_row_slots_a=read_a.blank_row_slots,
        blank_row_slots_b=read_b.blank_row_slots,
        row_count_a=len(read_a.rows),
        row_count_b=len(read_b.rows),
        rows=rows,
    )
