"""Runs F-04 on the pack for the normalise tests (contracts/normalise.md)."""

from collections.abc import Sequence
from uuid import UUID

from app.domain.models import CanonicalRow, RegisterPageInput, ReviewItem
from app.pipeline.engine import EngineInputs
from app.pipeline.normalise import NormaliseResult, RawRow, normalise_all, read_erp, read_sheet

PACK_RUN_ID = UUID("00000000-0000-4000-8000-000000002609")
"""Same fixed run id as `tests/conftest.py`, so stable ids match across tests."""


def normalise_pack(
    inputs: EngineInputs,
    *,
    erp: Sequence[RawRow] | None = None,
    sheet: Sequence[RawRow] | None = None,
    pages: Sequence[RegisterPageInput] | None = None,
    decisions: Sequence[ReviewItem] | None = None,
) -> NormaliseResult:
    """`normalise_all` on `inputs`, with any of the sources or the decisions replaced."""
    return normalise_all(
        inputs.run_id,
        inputs.month,
        list(inputs.register_pages if pages is None else pages),
        list(read_erp(inputs.erp_bytes) if erp is None else erp),
        list(read_sheet(inputs.sheet_bytes, inputs.month) if sheet is None else sheet),
        inputs.mappings,
        list(inputs.decisions if decisions is None else decisions),
    )


def row_by_id(result: NormaliseResult, source_row_id: str) -> CanonicalRow:
    """The canonical row with this `source_row_id` (exactly one)."""
    (row,) = [r for r in result.rows if r.source_row_id == source_row_id]
    return row


def with_values(rows: Sequence[RawRow], source_row_id: str, **changes: object) -> list[RawRow]:
    """A copy of `rows` where row `source_row_id` has `changes` merged into its values.

    Keys with spaces (sheet headers) are passed via `**{"Qty Received": ...}`.
    """
    assert any(r.source_row_id == source_row_id for r in rows), source_row_id
    return [
        RawRow(r.source_row_id, {**r.values, **changes}, r.line_no)
        if r.source_row_id == source_row_id
        else r
        for r in rows
    ]
