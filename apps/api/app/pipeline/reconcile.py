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
from app.domain.ids import grn_group_key, lot_group_key, review_item_key, stable_id
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
    for row in receipts:
        if row.grn_no is not None:
            key = grn_group_key(row.grn_no)
            groups.setdefault(key, _Members(key, row.grn_no, [])).rows.append(row)
    grn_groups = list(groups.values())

    unmatched: list[CanonicalRow] = []
    for row in receipts:
        if row.grn_no is not None:
            continue
        lot, supplier = row.lot_no, row.supplier_code
        if lot is not None and supplier is not None:
            candidates = [
                g
                for g in grn_groups
                if any(r.lot_no == lot and r.supplier_code == supplier for r in g.rows)
            ]
            if len(candidates) == 1:
                candidates[0].rows.append(row)
                continue
            key = lot_group_key(lot, supplier)
        else:
            key = f"ROW:{row.source_row_id}"
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
    if any(
        r.uom is not None and r.base_uom is not None and r.uom != r.base_uom for r in members.rows
    ):
        return Outcome.MATCHED_AFTER_CONVERSION
    return Outcome.MATCHED


def _review_item(
    run_id: UUID, row: CanonicalRow, members: _Members, reason: ReviewReason
) -> ReviewItem:
    key = review_item_key(ReviewKind.UNMATCHED_ROW, row_id=row.source_row_id)
    others = sorted(r.source_row_id for r in members.rows if r is not row)
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


# --- Entry point (R11) --------------------------------------------------------------------


def reconcile(run_id: UUID, rows: list[CanonicalRow]) -> ReconcileResult:
    """Give every row exactly one outcome and every group its diffs and outlier (F-05).

    Inputs are not mutated; new row instances are returned. Output is deterministic: groups by
    GRN (nulls last) then key, rows by source then row id, ids from `stable_id` (F-05.R11).
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
    placed: dict[str, tuple[UUID, Outcome]] = {}
    for members in members_list:
        firsts: dict[Source, CanonicalRow] = {}
        for row in members.rows:
            firsts.setdefault(row.source, row)
        firsts = {s: firsts[s] for s in SOURCE_ORDER if s in firsts}
        diffs = _diffs(firsts)
        conflicts = _conflicts(members.rows)
        outcome = _outcome(members, diffs, bool(conflicts))
        group_id = stable_id(run_id, members.key)
        row_ids: dict[Source, list[str]] = {}
        for row in members.rows:
            row_ids.setdefault(row.source, []).append(row.source_row_id)
            placed[row.source_row_id] = (group_id, outcome)
            if row.source_row_id in unmatched_ids:
                review_items.append(
                    _review_item(run_id, row, members, ReviewReason.UNMATCHED_NO_KEY)
                )
        for row in conflicts:
            if row.source_row_id not in unmatched_ids:
                review_items.append(
                    _review_item(run_id, row, members, ReviewReason.CONFLICTING_ROWS_IN_SOURCE)
                )
        present = set(firsts)
        groups.append(
            Group(
                id=group_id,
                run_id=run_id,
                group_key=members.key,
                grn_no=members.grn_no,
                row_ids=row_ids,
                outcome=outcome,
                missing_from=(
                    [s for s in SOURCE_ORDER if s not in present]
                    if outcome is Outcome.MISSING
                    else []
                ),
                diffs=diffs,
            )
        )

    out_rows: list[CanonicalRow] = []
    for row in ordered:
        rid = row.source_row_id
        if rid in duplicate_of:
            update: dict[str, object] = {
                "row_kind": RowKind.DUPLICATE,
                "duplicate_of": duplicate_of[rid],
                "group_id": None,
                "outcome": Outcome.DUPLICATE,
            }
        elif rid in non_receipts:
            update = {
                "row_kind": RowKind.NON_RECEIPT,
                "duplicate_of": None,
                "group_id": None,
                "outcome": Outcome.NON_RECEIPT,
            }
        else:
            group_id, outcome = placed[rid]
            update = {
                "row_kind": RowKind.RECEIPT,
                "duplicate_of": None,
                "group_id": group_id,
                "outcome": outcome,
            }
        out_rows.append(row.model_copy(update=update))

    groups.sort(key=lambda g: (g.grn_no is None, g.grn_no or 0, g.group_key))
    review_items.sort(key=lambda i: i.item_key)
    return ReconcileResult(rows=out_rows, groups=groups, review_items=review_items)
