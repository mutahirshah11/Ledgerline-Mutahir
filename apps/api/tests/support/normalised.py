"""Runs F-04 on the pack for the normalise tests (contracts/normalise.md)."""

from collections.abc import Sequence
from uuid import UUID

from app.domain.enums import ReviewKind, ReviewReason, ReviewStatus, Source
from app.domain.ids import review_item_key, stable_id
from app.domain.models import CanonicalRow, RegisterPageInput, ReviewItem, UnknownMappingContext
from app.pipeline.engine import EngineInputs
from app.pipeline.normalise import NormaliseResult, RawRow, normalise_all, read_erp, read_sheet
from tests.factories import T0
from tests.support.pack import PACK_RUN_ID

__all__ = ["PACK_RUN_ID", "map_decision", "normalise_pack", "row_by_id", "with_values"]


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


def map_decision(
    run_id: UUID,
    row_id: str,
    field: str,
    value: str,
    source: Source,
    status: ReviewStatus = ReviewStatus.RESOLVED,
) -> ReviewItem:
    """An `UNKNOWN_MAPPING` decision `MAP:{row_id}:{field}` (`supplier`, `unit`, `date`)."""
    reason = {
        "supplier": ReviewReason.UNKNOWN_SUPPLIER,
        "unit": ReviewReason.UNKNOWN_UNIT,
        "date": ReviewReason.UNPARSEABLE_DATE,
    }[field]
    key = review_item_key(ReviewKind.UNKNOWN_MAPPING, row_id=row_id, field=field)
    return ReviewItem(
        id=stable_id(run_id, key),
        run_id=run_id,
        kind=ReviewKind.UNKNOWN_MAPPING,
        item_key=key,
        grn_no=None,
        source_row_id=row_id,
        field=field,
        reason=reason,
        read_a=None,
        read_b=None,
        context=UnknownMappingContext(source=source, raw=""),
        status=status,
        resolved_value=value if status is ReviewStatus.RESOLVED else None,
        resolved_by="reviewer@example.com",
        resolved_at=T0,
        note="cannot tell" if status is ReviewStatus.CANNOT_DETERMINE else None,
    )
