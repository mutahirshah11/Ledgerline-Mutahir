"""F-06 control checks (contracts/controls.md). Pure: no I/O, no clock; never touches outcomes."""

import calendar
import datetime as dt
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from uuid import UUID

from app.core.clock import KARACHI
from app.core.config import BACKDATE_DAYS
from app.domain.enums import FlagType, ReviewKind, ReviewReason, Source
from app.domain.ids import parse_month, review_item_key, stable_id
from app.domain.models import CanonicalRow, ControlFlag, FlagAckContext, ReviewItem
from app.pipeline.order import file_order, flag_order

ACK_FLAGS: frozenset[FlagType] = frozenset(
    {FlagType.SEQUENCE_GAP, FlagType.BACKDATED_ENTRY, FlagType.DATE_OUT_OF_PERIOD}
)
"""Flags that need a `FLAG_ACK` review item (FR-024); the others create none."""


@dataclass(frozen=True)
class ControlsResult:
    """Output of `run_controls` (data-model.md Section 2)."""

    flags: list[ControlFlag]
    review_items: list[ReviewItem]  # FLAG_ACK


# A flag before ids are assigned: (flag, grn_no, source_row_id, detail).
_Raw = tuple[FlagType, int | None, str | None, dict[str, str]]


# --- Rules (one per flag) -----------------------------------------------------------------


def _sequence_gaps(rows: Sequence[CanonicalRow]) -> Iterator[_Raw]:
    """One flag per contiguous range of missing GRNs between the lowest and highest GRN of any
    row, keyed by the range's first GRN (user decision 2026-10-07). Pairwise over the sorted
    distinct GRNs, so a typo like 999999 costs one flag, not one per number."""
    grns = sorted({r.grn_no for r in rows if r.grn_no is not None})
    for before, after in zip(grns, grns[1:], strict=False):
        if after - before > 1:
            low, high = before + 1, after - 1
            yield FlagType.SEQUENCE_GAP, low, None, {"low": str(low), "high": str(high)}


def _backdated(
    rows: Sequence[CanonicalRow], month_end: dt.date, backdate_days: int
) -> Iterator[_Raw]:
    """ERP entry dated (Asia/Karachi) after the month's last day, or > `backdate_days` after
    the GRN date."""
    for row in rows:
        entered = row.meta.entered_at
        if row.source is not Source.ERP or row.date is None or entered is None:
            continue
        local = entered.astimezone(KARACHI)
        days_after = (local.date() - row.date).days
        if local.date() > month_end or days_after > backdate_days:
            detail = {
                "grn_date": row.date.isoformat(),
                "entered_at": local.strftime("%Y-%m-%d %H:%M"),
                "days_after": str(days_after),
            }
            yield FlagType.BACKDATED_ENTRY, row.grn_no, row.source_row_id, detail


def _unusual_users(rows: Sequence[CanonicalRow]) -> Iterator[_Raw]:
    """ERP `entered_by` other than the most common one (tie: alphabetically first is usual)."""
    erp = [
        (r, r.meta.entered_by)
        for r in rows
        if r.source is Source.ERP and r.meta.entered_by is not None
    ]
    counts = Counter(user for _, user in erp)
    if not counts:
        return
    usual = min(counts, key=lambda user: (-counts[user], user))
    for row, user in erp:
        if user != usual:
            detail = {"entered_by": user, "usual": usual}
            yield FlagType.UNUSUAL_USER, row.grn_no, row.source_row_id, detail


def _sundays(rows: Sequence[CanonicalRow]) -> Iterator[_Raw]:
    """Any row (any source or kind) dated on a Sunday."""
    for row in rows:
        if row.date is not None and row.date.weekday() == calendar.SUNDAY:
            yield (
                FlagType.SUNDAY_DATE,
                row.grn_no,
                row.source_row_id,
                {"date": row.date.isoformat()},
            )


def _out_of_order(rows: Sequence[CanonicalRow]) -> Iterator[_Raw]:
    """SHEET and ERP: date earlier than the previous dated row of the source, in file order."""
    previous: dict[Source, CanonicalRow] = {}
    for row in rows:
        if row.source is Source.REGISTER or row.date is None:
            continue
        before = previous.get(row.source)
        if before is not None and before.date is not None and row.date < before.date:
            detail = {
                "date": row.date.isoformat(),
                "previous_row": before.source_row_id,
                "previous_date": before.date.isoformat(),
            }
            yield FlagType.OUT_OF_ORDER, row.grn_no, row.source_row_id, detail
        previous[row.source] = row


def _out_of_period(
    rows: Sequence[CanonicalRow], period: tuple[int, int], month: str
) -> Iterator[_Raw]:
    """Any row dated outside the run month `period` (year, month)."""
    for row in rows:
        if row.date is not None and (row.date.year, row.date.month) != period:
            detail = {"date": row.date.isoformat(), "month": month}
            yield FlagType.DATE_OUT_OF_PERIOD, row.grn_no, row.source_row_id, detail


# --- Entry point (C1, C2) -----------------------------------------------------------------


def _ack_item(run_id: UUID, flag: ControlFlag) -> ReviewItem:
    """The `FLAG_ACK` review item for one acknowledgeable flag, keyed and id'd stably."""
    key = review_item_key(
        ReviewKind.FLAG_ACK, flag=flag.flag, row_id=flag.source_row_id, grn_no=flag.grn_no
    )
    return ReviewItem(
        id=stable_id(run_id, key),
        run_id=run_id,
        kind=ReviewKind.FLAG_ACK,
        item_key=key,
        grn_no=flag.grn_no,
        source_row_id=flag.source_row_id,
        field=None,
        reason=ReviewReason(flag.flag.value),
        read_a=None,
        read_b=None,
        context=FlagAckContext(flag=flag.flag, detail=dict(flag.detail)),
    )


def run_controls(
    run_id: UUID,
    month: str,
    rows: list[CanonicalRow],
    backdate_days: int = BACKDATE_DAYS,
) -> ControlsResult:
    """Raise the six F-06 flags and their `FLAG_ACK` items for reconciled `rows`.

    Rows are only read (C1). Flags are sorted by (flag, GRN nulls last, row id); items by key;
    ids from `stable_id` (C2, research R8).
    """
    year, mon = parse_month(month)
    month_end = dt.date(year, mon, calendar.monthrange(year, mon)[1])
    ordered = sorted(rows, key=file_order)
    raw: list[_Raw] = [
        *_sequence_gaps(ordered),
        *_backdated(ordered, month_end, backdate_days),
        *_unusual_users(ordered),
        *_sundays(ordered),
        *_out_of_order(ordered),
        *_out_of_period(ordered, (year, mon), month),
    ]
    flags = sorted(
        (
            ControlFlag(
                id=stable_id(run_id, flag.value, row_id or str(grn)),
                run_id=run_id,
                flag=flag,
                grn_no=grn,
                source_row_id=row_id,
                detail=detail,
            )
            for flag, grn, row_id, detail in raw
        ),
        key=flag_order,
    )
    items = sorted(
        (_ack_item(run_id, f) for f in flags if f.flag in ACK_FLAGS), key=lambda i: i.item_key
    )
    return ControlsResult(flags=flags, review_items=items)
