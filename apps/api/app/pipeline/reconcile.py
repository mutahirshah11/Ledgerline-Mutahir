"""F-05 reconciliation (contracts/reconcile.md). Pure: no model calls, no I/O, no clock."""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from uuid import UUID

from app.domain.enums import (
    CellStatus,
    ComparedField,
    Outcome,
    ReviewKind,
    ReviewReason,
    RowKind,
    Source,
)
from app.domain.ids import (
    grn_group_key,
    lot_group_key,
    review_item_key,
    row_group_key,
    stable_id,
)
from app.domain.models import CanonicalRow, Diff, Group, ReviewItem, UnmatchedRowContext

SOURCE_ORDER: tuple[Source, ...] = (Source.REGISTER, Source.ERP, Source.SHEET)

_RETURN_REMARK = re.compile(r"\b(return|returned|rtv)\b", re.IGNORECASE)
_DIGITS = re.compile(r"\d+")

KEY_FIELDS: tuple[str, ...] = ("date", "grn_no", "supplier_code", "lot_no", "qty", "uom")
"""Compared or key fields whose non-`CLEAR` cell sends a group to review (F-05.R7 rule 1)."""


@dataclass(frozen=True)
class ReconcileResult:
    """Output of `reconcile` (data-model.md Section 2)."""

    rows: list[CanonicalRow]  # row_kind, duplicate_of, group_id, outcome set
    groups: list[Group]
    review_items: list[ReviewItem]  # UNMATCHED_ROW


def _file_order(row: CanonicalRow) -> tuple[int, tuple[int, ...]]:
    """Source order, then the numbers in the row id (page, row / line / Excel row), so
    `XLS-r9` sorts before `XLS-r10` (F-05.R11; contracts/normalise.md N11)."""
    numbers = tuple(int(n) for n in _DIGITS.findall(row.source_row_id))
    return SOURCE_ORDER.index(row.source), numbers


def _values(row: CanonicalRow) -> tuple[object, ...]:
    """The canonical fields that make two rows identical (Clarification 1)."""
    return (row.grn_no, row.date, row.supplier_code, row.item, row.lot_no, row.qty, row.uom)


# --- R1, R2: duplicates and non-receipts --------------------------------------------------


def _duplicates(rows: Sequence[CanonicalRow]) -> dict[str, str]:
    """source_row_id → kept row id for later identical rows of one source and GRN (F-05.R1)."""
    kept: dict[tuple[Source, int], list[CanonicalRow]] = {}
    duplicate_of: dict[str, str] = {}
    for row in rows:
        if row.grn_no is None:
            continue
        earlier = kept.setdefault((row.source, row.grn_no), [])
        match = next((k for k in earlier if _values(k) == _values(row)), None)
        if match is None:
            earlier.append(row)
        else:
            duplicate_of[row.source_row_id] = match.source_row_id
    return duplicate_of


def _is_non_receipt(row: CanonicalRow) -> bool:
    """Qty below zero, or a remark naming a return (F-05.R2)."""
    if row.qty is not None and row.qty < 0:
        return True
    return row.meta.remarks is not None and bool(_RETURN_REMARK.search(row.meta.remarks))


# --- R3, R4: grouping ---------------------------------------------------------------------


@dataclass
class _Members:
    key: str
    grn_no: int | None
    rows: list[CanonicalRow]
    fallback: bool = False


def _group_rows(receipts: Sequence[CanonicalRow]) -> tuple[list[_Members], list[CanonicalRow]]:
    """GRN groups, then no-GRN rows by the fallback key (F-05.R3, R4).

    Returns the groups and the rows that fell to their own fallback group (`UNMATCHED_NO_KEY`).
    """
    groups: dict[str, _Members] = {}
    grn_keys_by_lot: dict[tuple[str, str], set[str]] = {}  # (lot, supplier) -> GRN group keys
    for row in receipts:
        if row.grn_no is not None:
            key = grn_group_key(row.grn_no)
            groups.setdefault(key, _Members(key, row.grn_no, [])).rows.append(row)
            if row.lot_no is not None and row.supplier_code is not None:
                grn_keys_by_lot.setdefault((row.lot_no, row.supplier_code), set()).add(key)

    unmatched: list[CanonicalRow] = []
    for row in receipts:
        if row.grn_no is not None:
            continue
        lot, supplier = row.lot_no, row.supplier_code
        if lot is not None and supplier is not None:
            candidates = grn_keys_by_lot.get((lot, supplier), set())
            if len(candidates) == 1:
                groups[next(iter(candidates))].rows.append(row)
                continue
            key = lot_group_key(lot, supplier)
        else:
            key = row_group_key(row.source_row_id)
        groups.setdefault(key, _Members(key, None, [], fallback=True)).rows.append(row)
        unmatched.append(row)
    for members in groups.values():
        members.rows.sort(key=_file_order)
    return list(groups.values()), unmatched


# --- R6, R8: diffs ------------------------------------------------------------------------

_Render = Callable[[CanonicalRow], str | None]


def _qty_value(row: CanonicalRow) -> str | None:
    if row.qty is None:
        return None
    qty = format(row.qty, "f")
    return qty if row.uom is None else f"{qty} {row.uom}"


def _qty_compared(row: CanonicalRow) -> str | None:
    if row.qty_base is None or row.base_uom is None:
        return None
    return f"{row.qty_base:.2f} {row.base_uom}"


def _iso(row: CanonicalRow) -> str | None:
    return None if row.date is None else row.date.isoformat()


_FIELD_RENDER: dict[ComparedField, tuple[_Render, _Render]] = {
    ComparedField.QTY: (_qty_value, _qty_compared),
    ComparedField.UOM: (lambda r: r.uom, lambda r: r.base_uom),
    ComparedField.LOT_NO: (lambda r: r.lot_no, lambda r: r.lot_no),
    ComparedField.DATE: (_iso, _iso),
    ComparedField.SUPPLIER_CODE: (lambda r: r.supplier_code, lambda r: r.supplier_code),
}


def _diff(field: ComparedField, firsts: dict[Source, CanonicalRow]) -> Diff | None:
    """A diff when two non-null compared values differ; nulls take no part (Clarification 2).

    Outlier: the one source that differs when the other two non-null values agree."""
    render_value, render_compared = _FIELD_RENDER[field]
    compared = {s: render_compared(r) for s, r in firsts.items()}
    known = {s: v for s, v in compared.items() if v is not None}
    if len(set(known.values())) < 2:
        return None
    outlier = None
    if len(known) == 3:
        for source, value in known.items():
            others = [v for s, v in known.items() if s is not source]
            if others[0] == others[1] != value:
                outlier = source
                break
    return Diff(
        field=field,
        values={s: render_value(r) for s, r in firsts.items()},
        compared=compared,
        outlier=outlier,
    )


def _diffs(firsts: dict[Source, CanonicalRow]) -> list[Diff]:
    """Diffs in F-09.R1 order: `qty` or `uom`, `lot_no`, `date`, `supplier_code` (F-05.R6, R8).

    Different base units give a `uom` diff and no `qty` diff (count units never convert)."""
    if len(firsts) < 2:
        return []
    bases = {r.base_uom for r in firsts.values() if r.base_uom is not None}
    qty_field = ComparedField.UOM if len(bases) > 1 else ComparedField.QTY
    fields = (qty_field, ComparedField.LOT_NO, ComparedField.DATE, ComparedField.SUPPLIER_CODE)
    return [d for f in fields if (d := _diff(f, firsts)) is not None]


# --- R5, R7: outcomes ---------------------------------------------------------------------


def _conflicts(rows: Sequence[CanonicalRow]) -> list[CanonicalRow]:
    """Later rows of a source that differ from an earlier row of that source (F-05.R5)."""
    seen: dict[Source, list[CanonicalRow]] = {}
    later: list[CanonicalRow] = []
    for row in rows:
        earlier = seen.setdefault(row.source, [])
        if any(_values(e) != _values(row) for e in earlier):
            later.append(row)
        earlier.append(row)
    return later


def _outcome(members: _Members, diffs: list[Diff], conflict: bool) -> Outcome:
    """First rule that applies (F-05.R7; data-model Section 4)."""
    unclear = any(
        r.cell_status.get(f, CellStatus.CLEAR) is not CellStatus.CLEAR
        for r in members.rows
        for f in KEY_FIELDS
    )
    if unclear or conflict:
        return Outcome.NEEDS_REVIEW
    if members.fallback:
        return Outcome.UNMATCHED_NO_KEY
    if len({r.source for r in members.rows}) < len(SOURCE_ORDER):
        return Outcome.MISSING
    if diffs:
        return Outcome.MISMATCHED
    # uom != base_uom is "factor != 1" (data-model Section 4): canonical codes have factor 1
    # exactly when the unit is its own base.
    if any(
        r.uom is not None and r.base_uom is not None and r.uom != r.base_uom for r in members.rows
    ):
        return Outcome.MATCHED_AFTER_CONVERSION
    return Outcome.MATCHED


def _review_item(
    run_id: UUID, row: CanonicalRow, members: _Members, reason: ReviewReason
) -> ReviewItem:
    key = review_item_key(ReviewKind.UNMATCHED_ROW, row_id=row.source_row_id)
    others = [r.source_row_id for r in members.rows if r is not row]  # file order
    return ReviewItem(
        id=stable_id(run_id, key),
        run_id=run_id,
        kind=ReviewKind.UNMATCHED_ROW,
        item_key=key,
        grn_no=row.grn_no,
        source_row_id=row.source_row_id,
        field=None,
        reason=reason,
        read_a=None,
        read_b=None,
        context=UnmatchedRowContext(group_key=members.key, other_rows=others),
    )


def _build_group(
    run_id: UUID, members: _Members, unmatched_ids: set[str]
) -> tuple[Group, list[ReviewItem]]:
    """The group with its diffs and outcome, and its `UNMATCHED_ROW` review items (R4 to R8)."""
    firsts: dict[Source, CanonicalRow] = {}
    row_ids: dict[Source, list[str]] = {}
    for row in members.rows:  # file order, so sources come REGISTER, ERP, SHEET
        firsts.setdefault(row.source, row)
        row_ids.setdefault(row.source, []).append(row.source_row_id)
    diffs = _diffs(firsts)
    conflicts = _conflicts(members.rows)
    outcome = _outcome(members, diffs, bool(conflicts))
    items = [
        _review_item(run_id, row, members, ReviewReason.UNMATCHED_NO_KEY)
        for row in members.rows
        if row.source_row_id in unmatched_ids
    ] + [
        _review_item(run_id, row, members, ReviewReason.CONFLICTING_ROWS_IN_SOURCE)
        for row in conflicts
        if row.source_row_id not in unmatched_ids
    ]
    group = Group(
        id=stable_id(run_id, "GROUP", members.key),
        run_id=run_id,
        group_key=members.key,
        grn_no=members.grn_no,
        row_ids=row_ids,
        outcome=outcome,
        missing_from=(
            [s for s in SOURCE_ORDER if s not in firsts] if outcome is Outcome.MISSING else []
        ),
        diffs=diffs,
    )
    return group, items


# --- Entry point (R11) --------------------------------------------------------------------


def reconcile(run_id: UUID, rows: list[CanonicalRow]) -> ReconcileResult:
    """Give every row exactly one outcome and every group its diffs and outlier (F-05).

    Inputs are not mutated; new row instances are returned. Output is deterministic: groups by
    GRN (nulls last) then key, rows by source then row id, review items in file order of their
    row, ids from `stable_id` (F-05.R11).
    """
    ordered = sorted(rows, key=_file_order)
    duplicate_of = _duplicates(ordered)
    non_receipts = {
        r.source_row_id
        for r in ordered
        if r.source_row_id not in duplicate_of and _is_non_receipt(r)
    }
    receipts = [
        r
        for r in ordered
        if r.source_row_id not in duplicate_of and r.source_row_id not in non_receipts
    ]
    members_list, unmatched = _group_rows(receipts)
    unmatched_ids = {r.source_row_id for r in unmatched}

    groups: list[Group] = []
    review_items: list[ReviewItem] = []
    placed: dict[str, Group] = {}
    for members in members_list:
        group, items = _build_group(run_id, members, unmatched_ids)
        groups.append(group)
        review_items.extend(items)
        placed.update((row.source_row_id, group) for row in members.rows)

    out_rows: list[CanonicalRow] = []
    for row in ordered:
        rid = row.source_row_id
        choice: tuple[RowKind, str | None, UUID | None, Outcome]
        if rid in duplicate_of:
            choice = (RowKind.DUPLICATE, duplicate_of[rid], None, Outcome.DUPLICATE)
        elif rid in non_receipts:
            choice = (RowKind.NON_RECEIPT, None, None, Outcome.NON_RECEIPT)
        else:
            choice = (RowKind.RECEIPT, None, placed[rid].id, placed[rid].outcome)
        fields = ("row_kind", "duplicate_of", "group_id", "outcome")
        out_rows.append(row.model_copy(update=dict(zip(fields, choice, strict=True))))

    groups.sort(key=lambda g: (g.grn_no is None, g.grn_no or 0, g.group_key))
    rank = {r.source_row_id: n for n, r in enumerate(ordered)}
    # UNMATCHED_ROW items always carry their row id.
    review_items.sort(key=lambda i: rank[i.source_row_id or ""])
    return ReconcileResult(rows=out_rows, groups=groups, review_items=review_items)
