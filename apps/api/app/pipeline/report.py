"""F-09 exception list, line templates and headline (005 data-model.md §3, research R5, R6;
contracts/telegram-message.md "Line templates"; blueprint F-09.R1, R2, C4, C5).

Pure: no I/O, no clock, no model. One function (`exception_list`) orders what needs attention for
the run overview (EP-05), the report and the summary message; every line is a fixed template
filled only with run data (F-09.R2). The frozen `report.html` (template
`app/templates/report.html.j2`) and `outcomes.csv` are rendered here from a `ReportContext`
(F-09.R3, R4); `services/finalise.py` stores them once.
"""

import csv
import datetime as dt
import io
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as default
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from app.core.clock import KARACHI
from app.domain.enums import (
    AuditAction,
    ComparedField,
    FlagType,
    NormCode,
    Outcome,
    ReviewKind,
    ReviewReason,
    ReviewStatus,
    RowKind,
    Source,
)
from app.domain.ids import parse_month
from app.domain.models import (
    AuditEntry,
    CanonicalRow,
    CellContext,
    ControlFlag,
    ExceptionItem,
    ExceptionKind,
    Group,
    GroupRowView,
    GroupView,
    NormLogEntry,
    PageLabelMismatchContext,
    Proof,
    ReviewItem,
    RowInOneReadContext,
    Run,
    SourceFile,
    UnknownMappingContext,
)
from app.pipeline.order import SOURCE_ORDER

# --- vocabulary (research R6) --------------------------------------------------------------

SOURCE_LABEL: dict[Source, str] = {
    Source.REGISTER: "register",
    Source.ERP: "ERP",
    Source.SHEET: "sheet",
}
"""Source labels inside a line."""
SOURCE_TITLE: dict[Source, str] = {
    Source.REGISTER: "Register",
    Source.ERP: "ERP",
    Source.SHEET: "Sheet",
}
"""Source labels starting an "Also:" phrase."""
DIFF_LABEL: dict[ComparedField, str] = {
    ComparedField.QTY: "qty",
    ComparedField.UOM: "unit",
    ComparedField.LOT_NO: "lot",
    ComparedField.DATE: "date",
    ComparedField.SUPPLIER_CODE: "supplier",
}
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_ROW_PREFIX: dict[str, Source] = {"REG-": Source.REGISTER, "ERP-": Source.ERP, "XLS-": Source.SHEET}
"""Row id prefixes (`app.domain.ids`), so the "Also:" line needs only the exception items."""
_CAUSES = ("ink blot", "smudge", "stain", "torn", "tear", "faded", "overwritten", "blot")
"""What covers an illegible cell, as the read's note names it (F-03 R3: "ink blot",
"overwritten"); the first one found in the note is shown, else the note's first clause."""

# --- ranks (data-model.md §3) --------------------------------------------------------------

NO_GRN = 10**9
_DIFF_RANK = {
    ComparedField.QTY: 0,
    ComparedField.UOM: 0,
    ComparedField.LOT_NO: 1,
    ComparedField.DATE: 2,
    ComparedField.SUPPLIER_CODE: 3,
}
_REVIEW_RANK = {  # Clarification 1
    ReviewReason.ILLEGIBLE: 0,
    ReviewReason.UNCLEAR: 1,
    ReviewReason.READS_DISAGREE: 1,
    ReviewReason.ROW_IN_ONE_READ: 2,
    ReviewReason.PAGE_LABEL_MISMATCH: 2,
    ReviewReason.CONFLICTING_ROWS_IN_SOURCE: 3,
    ReviewReason.UNMATCHED_NO_KEY: 3,
    ReviewReason.CORRECTED: 4,
}
_UNKNOWN_MAPPING_RANK = 3  # not in Clarification 1: ranked with the other source-row problems
_UNDETERMINED_RANK = 5
_ACK_RANK = {FlagType.SEQUENCE_GAP: 0, FlagType.BACKDATED_ENTRY: 1, FlagType.DATE_OUT_OF_PERIOD: 2}
_FLAG_INDEX = {f: n for n, f in enumerate(FlagType)}
_ROW_ONLY_RANK = {RowKind.DUPLICATE: 0, RowKind.NON_RECEIPT: 1}
_OTHER_FLAG_RANK = 2  # + FlagType declaration order
_GROUP_CATEGORY: dict[Outcome, tuple[int, ExceptionKind]] = {
    Outcome.MISSING: (1, "MISSING"),
    Outcome.MISMATCHED: (2, "MISMATCH"),
    Outcome.NEEDS_REVIEW: (3, "REVIEW"),
    Outcome.UNMATCHED_NO_KEY: (3, "UNMATCHED"),
}
MATCH_OUTCOMES = frozenset({Outcome.MATCHED, Outcome.MATCHED_AFTER_CONVERSION})

Rows = Mapping[str, CanonicalRow]
"""Canonical rows by `source_row_id`."""
_Render = Callable[[list[ControlFlag]], str]
"""Renders a line from the flags attached to it."""


# --- formatting ----------------------------------------------------------------------------


def fmt_date(value: dt.date) -> str:
    """`{d} {Mon}`, e.g. `2 Oct`."""
    return f"{value.day} {_MONTHS[value.month - 1]}"


def fmt_qty(value: Decimal) -> str:
    """As recorded, without trailing zeros, never in exponent form (`1200.00` → `1200`)."""
    text = format(value.normalize(), "f")
    return text


def month_label(month: str) -> str:
    """`2026-09` → `Sep 2026`."""
    year, mon = parse_month(month)
    return f"{_MONTHS[mon - 1]} {year}"


def _iso_date(text: str | None) -> dt.date | None:
    try:
        return None if text is None else dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


def _date_text(text: str | None) -> str:
    found = _iso_date(text)
    return fmt_date(found) if found is not None else (text or "")


def _qty_text(text: str) -> str:
    """A diff's `{qty} {uom}` value with the quantity normalised (`70.00 DRUM` → `70 DRUM`)."""
    number, _, unit = text.partition(" ")
    try:
        qty = fmt_qty(Decimal(number))
    except InvalidOperation:
        return text
    return f"{qty} {unit}" if unit else qty


def _row_source(row_id: str | None) -> Source | None:
    return next((s for p, s in _ROW_PREFIX.items() if row_id and row_id.startswith(p)), None)


def _flag_key(flag: ControlFlag) -> tuple[int, str, str]:
    """FlagType declaration order, then row id (data-model.md §3 attached flag order)."""
    return _FLAG_INDEX[flag.flag], flag.source_row_id or "", str(flag.grn_no or "")


def _group_name(group: Group) -> str:
    if group.grn_no is not None:
        return str(group.grn_no)
    first = next((ids[0] for s in SOURCE_ORDER if (ids := group.row_ids.get(s))), None)
    return first or group.group_key


def _first_row(group: Group, rows: Rows) -> CanonicalRow | None:
    for source in SOURCE_ORDER:
        for row_id in group.row_ids.get(source, []):
            if row_id in rows:
                return rows[row_id]
    return None


# --- line templates (contracts/telegram-message.md) ----------------------------------------


def _backdated_clause(flags: Sequence[ControlFlag], rows: Rows) -> str:
    """`, dated {grn_date}, keyed {entered_at date} by {entered_by}` for an attached
    `BACKDATED_ENTRY` (the user from `UNUSUAL_USER` on the same row, else the row)."""
    out = ""
    for f in flags:
        if f.flag is not FlagType.BACKDATED_ENTRY:
            continue
        user = _keyed_by(f, flags, rows)
        out += f", dated {_date_text(f.detail.get('grn_date'))}"
        out += f", keyed {_date_text(f.detail.get('entered_at'))}" + (f" by {user}" if user else "")
    return out


def _keyed_by(flag: ControlFlag, flags: Sequence[ControlFlag], rows: Rows) -> str | None:
    for other in flags:
        if other.flag is FlagType.UNUSUAL_USER and other.source_row_id == flag.source_row_id:
            return other.detail.get("entered_by")
    row = rows.get(flag.source_row_id or "")
    return None if row is None else row.meta.entered_by


def line_missing(group: Group, rows: Rows, flags: Sequence[ControlFlag]) -> str:
    """Category 1: `{grn} {item}, {qty} {uom}: {SRC} only` or `…: in {a} + {b}, NOT in {c}`,
    then the attached flag clauses."""
    present = [s for s in SOURCE_ORDER if group.row_ids.get(s)]
    head = _group_name(group)
    first = _first_row(group, rows)
    if first is not None and first.item:
        head += f" {first.item.lower()}"
    if first is not None and first.qty is not None:
        head += f", {fmt_qty(first.qty)}" + (f" {first.uom}" if first.uom else "")
    labels = " + ".join(SOURCE_LABEL[s] for s in present)
    if len(present) == 1:
        where = f"{labels} only"
    else:
        where = f"in {labels}, NOT in " + " + ".join(SOURCE_LABEL[s] for s in group.missing_from)
    return f"{head}: {where}{_backdated_clause(flags, rows)}"


def line_diff(group: Group, flags: Sequence[ControlFlag]) -> str:
    """Category 2: `{grn} {label}: {outlier} {v}, {a} + {b} {v2}` (agreeing values textually
    equal), `…, {a} {va} = {b} {vb}` (equal only after conversion) or, with no outlier,
    `{label}: register {r}, ERP {e}, sheet {s}` (null values take no part); further diffs
    follow as `; {label}: …`. Dates get ` (Sun)` when that row has an attached `SUNDAY_DATE`."""
    sundays = {f.source_row_id for f in flags if f.flag is FlagType.SUNDAY_DATE}
    parts: list[str] = []
    for diff in group.diffs:
        shown: dict[Source, str] = {}
        for source in SOURCE_ORDER:
            value = diff.values.get(source)
            if value is None:
                continue
            if diff.field is ComparedField.QTY:
                value = _qty_text(value)
            elif diff.field is ComparedField.DATE:
                value = _date_text(value)
                if set(group.row_ids.get(source, [])[:1]) & sundays:
                    value += " (Sun)"
            shown[source] = value
        label = DIFF_LABEL[diff.field]
        out = diff.outlier
        if out is not None and out in shown and len(shown) == 3:
            a, b = (s for s in shown if s is not out)
            if shown[a] == shown[b]:
                agree = f"{SOURCE_LABEL[a]} + {SOURCE_LABEL[b]} {shown[a]}"
            else:
                agree = f"{SOURCE_LABEL[a]} {shown[a]} = {SOURCE_LABEL[b]} {shown[b]}"
            parts.append(f"{label}: {SOURCE_LABEL[out]} {shown[out]}, {agree}")
        else:
            listed = ", ".join(f"{SOURCE_LABEL[s]} {v}" for s, v in shown.items())
            parts.append(f"{label}: {listed}")
    return f"{_group_name(group)} " + "; ".join(parts)


def _cause(note: str) -> str:
    lowered = note.lower()
    found = [(lowered.find(c), c) for c in _CAUSES if c in lowered]
    if found:
        return min(found)[1]
    return re.split(r"[;|]", note, maxsplit=1)[0].strip()


def _read_text(item: ReviewItem, which: str) -> str:
    cell = item.read_a if which == "a" else item.read_b
    if cell is None:
        return "none"
    if cell.value is not None:
        return cell.value
    return " or ".join(cell.candidates) or "none"


def line_review(item: ReviewItem, rows: Rows) -> str:
    """Category 3, from the group's most severe open or undetermined review item."""
    who = str(item.grn_no) if item.grn_no is not None else (item.source_row_id or "")
    fld = item.field or "row"
    ctx = item.context
    if item.status is ReviewStatus.CANNOT_DETERMINE:
        return f"{who} {fld} could not be determined: {item.note or ''}".rstrip(": ")
    reason = item.reason
    if reason is ReviewReason.ILLEGIBLE:
        notes = [n for n in (item.read_a and item.read_a.note, getattr(ctx, "note", None)) if n]
        return f"{who} {fld} unreadable on register" + (f" ({_cause(notes[0])})" if notes else "")
    if reason is ReviewReason.CORRECTED:
        struck = ctx.struck if isinstance(ctx, CellContext) else None
        current = next((c.value for c in (item.read_a, item.read_b) if c and c.value), None)
        return f"{who} {fld} corrected on register, {struck or 'none'} to {current or 'none'}"
    if reason in (ReviewReason.UNCLEAR, ReviewReason.READS_DISAGREE):
        reads = f"{_read_text(item, 'a')} / {_read_text(item, 'b')}"
        return f"{who} {fld} unclear on register (reads: {reads})"
    if reason is ReviewReason.ROW_IN_ONE_READ and isinstance(ctx, RowInOneReadContext):
        return f"{who} row read only once on register page {ctx.page_no}"
    if reason is ReviewReason.PAGE_LABEL_MISMATCH and isinstance(ctx, PageLabelMismatchContext):
        return f"register page {ctx.page_no} label does not match the run"
    row = rows.get(item.source_row_id or "")
    source = SOURCE_LABEL[row.source] if row is not None else "a source"
    if reason is ReviewReason.CONFLICTING_ROWS_IN_SOURCE:
        return f"{who}: {source} has two different rows"
    if reason is ReviewReason.UNMATCHED_NO_KEY:
        lot = row.lot_no if row is not None else None
        return f"{item.source_row_id}: no GRN, lot {lot or 'none'} not matched"
    raw = ctx.raw if isinstance(ctx, UnknownMappingContext) else ""
    return f"{who} {fld} not recognised on {source} ({raw})"


def _line_unmatched(group: Group, rows: Rows) -> str:
    row = _first_row(group, rows)
    lot = row.lot_no if row is not None else None
    return f"{_group_name(group)}: no GRN, lot {lot or 'none'} not matched"


def line_flag(flag: ControlFlag, rows: Rows, flags: Sequence[ControlFlag] = ()) -> str:
    """Categories 4 and 5: a flag not attached to a category 1 to 3 line (`flags`: those
    attached to it, e.g. `UNUSUAL_USER` on a backdated row)."""
    d = flag.detail
    where = flag.source_row_id or f"GRN {flag.grn_no}"
    match flag.flag:
        case FlagType.SEQUENCE_GAP:
            low, high = d.get("low", str(flag.grn_no)), d.get("high", str(flag.grn_no))
            span = f"GRN {low}" if low == high else f"GRNs {low} to {high}"
            return f"{span} missing everywhere"
        case FlagType.BACKDATED_ENTRY:
            user = _keyed_by(flag, flags, rows)
            by = f" by {user}" if user else ""
            keyed = _date_text(d.get("entered_at"))
            return f"GRN {flag.grn_no} keyed {keyed}{by}, dated {_date_text(d.get('grn_date'))}"
        case FlagType.DATE_OUT_OF_PERIOD:
            month = d.get("month")
            outside = f", outside {month_label(month)}" if month else ""
            return f"{where} dated {_date_text(d.get('date'))}{outside}"
        case FlagType.UNUSUAL_USER:
            return f"{where} keyed by {d.get('entered_by')}, usually {d.get('usual')}"
        case FlagType.SUNDAY_DATE:
            return f"{where} dated {_date_text(d.get('date'))} (Sun)"
        case FlagType.OUT_OF_ORDER:
            prev = f"{d.get('previous_row')} dated {_date_text(d.get('previous_date'))}"
            return f"{where} dated {_date_text(d.get('date'))}, after {prev}"
        case FlagType.UNKNOWN_SUPPLIER:
            return f"{where} supplier not on the map ({d.get('raw', '')})"
        case FlagType.UNKNOWN_UNIT:
            return f"{where} unit not on the map ({d.get('raw', '')})"


def _lists(source: Source) -> str:
    return f"{SOURCE_TITLE[source]} lists "


def _line_duplicate(row: CanonicalRow, rows: Sequence[CanonicalRow]) -> str:
    """`{Source} lists {grn} twice` / `{k} times`."""
    times = 1 + sum(1 for r in rows if r.duplicate_of and r.duplicate_of == row.duplicate_of)
    count = "twice" if times == 2 else f"{times} times"
    name = str(row.grn_no) if row.grn_no is not None else (row.duplicate_of or row.source_row_id)
    return f"{_lists(row.source)}{name} {count}"


def _line_return(row: CanonicalRow) -> str:
    """`{Source} lists one return ({qty} {uom})`."""
    qty = "" if row.qty is None else f" ({fmt_qty(row.qty)}" + (f" {row.uom})" if row.uom else ")")
    return f"{_lists(row.source)}one return{qty}"


def also_phrases(items: Sequence[ExceptionItem]) -> list[str]:
    """The "Also:" phrases: every category 4 item, then per source its duplicates and returns
    joined with ` and ` (`Sheet lists 4409 twice and one return (-5000 PCS)`), in list order.
    Other category 5 flags are not phrased (contracts/telegram-message.md)."""
    phrases = [i.text for i in items if i.category == 4]
    by_source: dict[Source, tuple[list[str], list[str]]] = {}
    for item in items:
        source = _row_source(item.source_row_id)
        if item.kind not in ("DUPLICATE", "NON_RECEIPT") or source is None:
            continue
        dups, returns = by_source.setdefault(source, ([], []))
        part = item.text.removeprefix(_lists(source))
        if item.kind == "DUPLICATE":
            if part not in dups:
                dups.append(part)
        else:
            returns.append(part)
    for source, (dups, returns) in by_source.items():
        parts = dups + (returns if len(returns) == 1 else [f"{len(returns)} returns"])
        phrases.append(_lists(source) + " and ".join(p for p in parts if p))
    return phrases


def also_line(items: Sequence[ExceptionItem]) -> str | None:
    """`Also: {phrase}. {phrase}.`, or None when there is nothing to add."""
    phrases = also_phrases(items)
    return "Also: " + " ".join(f"{p}." for p in phrases) if phrases else None


def headline(groups: Sequence[Group]) -> str:
    """`{n} receipts: {m} match, {k} need attention` (FR-020): match = MATCHED +
    MATCHED_AFTER_CONVERSION groups, need attention = every other group."""
    matched = sum(1 for g in groups if g.outcome in MATCH_OUTCOMES)
    noun = "receipt" if len(groups) == 1 else "receipts"
    return f"{len(groups)} {noun}: {matched} match, {len(groups) - matched} need attention"


# --- the exception list (research R5) ------------------------------------------------------


@dataclass
class _Draft:
    category: int
    rank: int
    kind: ExceptionKind
    grn_no: int | None
    group_key: str | None
    source_row_id: str | None
    field: str | None
    render: _Render
    flags: list[ControlFlag] = default(default_factory=list)

    def key(self) -> tuple[int, int, int, str, str, str]:
        grn = NO_GRN if self.grn_no is None else self.grn_no
        first = str(self.flags[0].id) if self.flags else ""
        return (
            self.category,
            self.rank,
            grn,
            self.source_row_id or "",
            self.group_key or "",
            first,
        )

    def item(self) -> ExceptionItem:
        flags = sorted(self.flags, key=_flag_key)
        return ExceptionItem(
            category=self.category,
            kind=self.kind,
            grn_no=self.grn_no,
            group_key=self.group_key,
            source_row_id=self.source_row_id,
            field=self.field,
            flags=flags,
            text=self.render(flags),
        )


def _review_rank(item: ReviewItem) -> int:
    if item.status is ReviewStatus.CANNOT_DETERMINE:
        return _UNDETERMINED_RANK
    return _REVIEW_RANK.get(item.reason, _UNKNOWN_MAPPING_RANK)


def _group_draft(group: Group, rows: Rows, items: Sequence[ReviewItem]) -> _Draft | None:
    if group.outcome not in _GROUP_CATEGORY:
        return None
    category, kind = _GROUP_CATEGORY[group.outcome]

    def draft(rank: int, fld: str | None, render: _Render, row_id: str | None = None) -> _Draft:
        return _Draft(category, rank, kind, group.grn_no, group.group_key, row_id, fld, render)

    if kind == "MISSING":
        return draft(0, None, lambda f: line_missing(group, rows, f))
    if kind == "MISMATCH":
        diff = group.diffs[0].field if group.diffs else ComparedField.QTY
        return draft(_DIFF_RANK[diff], diff.value, lambda f: line_diff(group, f))
    ids = {i for ids in group.row_ids.values() for i in ids}
    mine = sorted(
        (
            i
            for i in items
            if i.source_row_id in ids
            and i.kind is not ReviewKind.FLAG_ACK
            and i.status is not ReviewStatus.RESOLVED
        ),
        key=lambda i: (_review_rank(i), i.item_key),
    )
    worst = mine[0] if mine else None
    rank = _review_rank(worst) if worst else _UNDETERMINED_RANK
    fld = worst.field if worst else None
    name = _group_name(group)
    render: _Render
    if worst is not None:
        found = worst
        render = lambda f: line_review(found, rows)  # noqa: E731
    elif kind == "UNMATCHED":
        render = lambda f: _line_unmatched(group, rows)  # noqa: E731
    else:
        render = lambda f: f"{name} needs review"  # noqa: E731
    first = _first_row(group, rows) if kind == "UNMATCHED" else None
    return draft(rank, fld, render, first.source_row_id if first else None)


def exception_list(
    run: Run,
    groups: Sequence[Group],
    rows: Sequence[CanonicalRow],
    flags: Sequence[ControlFlag],
    review_items: Sequence[ReviewItem],
    proof: Proof | None,
) -> list[ExceptionItem]:
    """The one ordered exception list (F-09.R1, FR-017, FR-018; data-model.md §3).

    Categories: 1 `MISSING`, 2 `MISMATCHED` (by first diff: qty/unit, lot, date, supplier),
    3 `NEEDS_REVIEW` / `UNMATCHED_NO_KEY` (Clarification 1), 4 acknowledgeable flags,
    5 duplicates, non-receipts and other flags; ties by GRN, then row id. A flag attaches to the
    row's own duplicate or non-receipt item, else to the category 1 to 3 item of its row's group
    or GRN (C5), else stands alone (category 4 or 5; later flags of a category 4 flag's row
    attach to it). `run` and `proof` are part of the contract for later templates; the list is
    computed on read and never stored (plan key point 3)."""
    by_id = {r.source_row_id: r for r in rows}
    drafts: list[_Draft] = []
    by_row: dict[str, _Draft] = {}
    by_grn: dict[int, _Draft] = {}
    for row in sorted(rows, key=lambda r: r.source_row_id):
        if row.row_kind in _ROW_ONLY_RANK:
            render = _bind_row(row, rows)
            kind: ExceptionKind = (
                "DUPLICATE" if row.row_kind is RowKind.DUPLICATE else "NON_RECEIPT"
            )
            rank = _ROW_ONLY_RANK[row.row_kind]
            own = _Draft(5, rank, kind, row.grn_no, None, row.source_row_id, None, render)
            drafts.append(own)
            by_row[row.source_row_id] = own
    for group in groups:
        line = _group_draft(group, by_id, review_items)
        if line is None:
            continue
        drafts.append(line)
        for ids in group.row_ids.values():
            for row_id in ids:
                by_row.setdefault(row_id, line)
        if group.grn_no is not None:
            by_grn.setdefault(group.grn_no, line)
    for flag in sorted(flags, key=_flag_key):
        target = by_row.get(flag.source_row_id or "")
        if target is None and flag.grn_no is not None:
            target = by_grn.get(flag.grn_no)
        if target is not None:
            target.flags.append(flag)
            continue
        alone = _flag_draft(flag, by_id)
        drafts.append(alone)
        if alone.category == 4 and flag.source_row_id is not None:
            by_row[flag.source_row_id] = alone
    return [d.item() for d in sorted(drafts, key=_Draft.key)]


def _bind_row(row: CanonicalRow, rows: Sequence[CanonicalRow]) -> _Render:
    if row.row_kind is RowKind.DUPLICATE:
        return lambda _: _line_duplicate(row, rows)
    return lambda _: _line_return(row)


def _flag_draft(flag: ControlFlag, rows: Rows) -> _Draft:
    if flag.flag in _ACK_RANK:
        category, rank = 4, _ACK_RANK[flag.flag]
    else:
        category, rank = 5, _OTHER_FLAG_RANK + _FLAG_INDEX[flag.flag]
    return _Draft(
        category,
        rank,
        "FLAG",
        flag.grn_no,
        None,
        flag.source_row_id,
        None,
        render=lambda attached: line_flag(flag, rows, attached),
        flags=[flag],
    )


# --- frozen report files (F-09.R3, R4; data-model.md §5, research R8, R10) ------------------

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
CSV_HEADER = (
    "source",
    "source_row_id",
    "grn_no",
    "outcome",
    "group_key",
    "diff_fields",
    "outlier",
    "row_kind",
)
NORM_EXAMPLES = 20
CATEGORY_TITLE = {
    1: "Missing from a source",
    2: "Sources disagree",
    3: "Waiting on review",
    4: "Control flags to acknowledge",
    5: "For information",
}
_FIELD_ROWS: tuple[tuple[str, tuple[ComparedField, ...]], ...] = (
    ("date", (ComparedField.DATE,)),
    ("supplier", (ComparedField.SUPPLIER_CODE,)),
    ("lot", (ComparedField.LOT_NO,)),
    ("qty", (ComparedField.QTY, ComparedField.UOM)),
)
"""Receipt table rows: label and the compared fields whose diffs mark its cells."""


def group_views(
    groups: Sequence[Group], rows: Sequence[CanonicalRow], flags: Sequence[ControlFlag]
) -> list[GroupView]:
    """EP-11's `GroupView`s (FR-021) in `groups` order: each source's rows sorted by id, with
    the flag types on each row."""
    by_id = {r.source_row_id: r for r in rows}
    on_row: dict[str, list[FlagType]] = {}
    for f in flags:
        if f.source_row_id is not None:
            on_row.setdefault(f.source_row_id, []).append(f.flag)
    out: list[GroupView] = []
    for group in groups:
        views = {
            source: [
                GroupRowView(
                    **by_id[row_id].model_dump(exclude={"id", "run_id", "group_id"}),
                    flags=on_row.get(row_id, []),
                )
                for row_id in sorted(ids)
                if row_id in by_id
            ]
            for source, ids in group.row_ids.items()
        }
        out.append(GroupView(group=group, rows=views))
    return out


@dataclass(frozen=True)
class ReportDecision:
    """A review item with the action and `equals_sources` of its `REVIEW_RESOLVED` audit
    entry (None / [] for an item still `OPEN`)."""

    item: ReviewItem
    action: str | None
    equals_sources: list[Source]


@dataclass(frozen=True)
class ReportContext:
    """Everything `report.html` prints (data-model.md §5); `run` carries the finalise instant
    and actor (research R8)."""

    run: Run
    month_label: str
    scenario_now: dt.datetime
    headline: str
    proof: Proof | None
    exceptions: list[ExceptionItem]
    groups: list[GroupView]
    flags: list[ControlFlag]
    decisions: list[ReportDecision]
    norm_summary: list[tuple[NormCode, int]]
    norm_examples: list[NormLogEntry]
    input_files: list[SourceFile]


def _decisions(items: Sequence[ReviewItem], audit: Sequence[AuditEntry]) -> list[ReportDecision]:
    """Decided items (status ≠ OPEN) in `items` order, then the open ones ("still open")."""
    newest: dict[str, AuditEntry] = {}
    for entry in audit:  # newest first
        if entry.action is AuditAction.REVIEW_RESOLVED:
            newest.setdefault(entry.entity_id, entry)
    decided = [i for i in items if i.status is not ReviewStatus.OPEN]
    still_open = [i for i in items if i.status is ReviewStatus.OPEN]
    out: list[ReportDecision] = []
    for item in decided + still_open:
        found = newest.get(str(item.id))
        after = found.after if found is not None else None
        if item.status is ReviewStatus.OPEN or not after:
            out.append(ReportDecision(item, None, []))
            continue
        equal = [Source(s) for s in after.get("equals_sources", [])]
        out.append(ReportDecision(item, after.get("action"), equal))
    return out


def build_report_context(
    run: Run,
    groups: Sequence[Group],
    rows: Sequence[CanonicalRow],
    flags: Sequence[ControlFlag],
    review_items: Sequence[ReviewItem],
    proof: Proof | None,
    norm_log: Sequence[NormLogEntry],
    input_files: Sequence[SourceFile],
    audit: Sequence[AuditEntry],
) -> ReportContext:
    """The `ReportContext` of a run about to be finalised (pure; `run.finalised_at` and
    `finalised_by` already set to the finalise instant and actor, research R8)."""
    counts: dict[NormCode, int] = {}
    for entry in norm_log:
        counts[entry.code] = counts.get(entry.code, 0) + 1
    order = list(Source)
    return ReportContext(
        run=run,
        month_label=month_label(run.month),
        scenario_now=run.scenario_now,
        headline=headline(groups),
        proof=proof,
        exceptions=exception_list(run, groups, rows, flags, review_items, proof),
        groups=group_views(groups, rows, flags),
        flags=list(flags),
        decisions=_decisions(review_items, audit),
        norm_summary=sorted(counts.items()),
        norm_examples=list(norm_log[:NORM_EXAMPLES]),
        input_files=sorted(input_files, key=lambda f: (order.index(f.source), f.page_no or 0)),
    )


def fmt_instant(value: dt.datetime | None) -> str:
    """`15 Oct 2026 08:30` in Asia/Karachi (no seconds); empty for None."""
    if value is None:
        return ""
    t = value.astimezone(KARACHI)
    return f"{fmt_date(t.date())} {t.year} {t.hour:02d}:{t.minute:02d}"


@dataclass(frozen=True)
class ReceiptCell:
    text: str
    differs: bool


@dataclass(frozen=True)
class ReceiptLine:
    label: str
    cells: dict[Source, ReceiptCell]
    outlier: str


def _row_value(row: GroupRowView, label: str) -> str | None:
    if label == "date":
        return None if row.date is None else fmt_date(row.date)
    if label == "supplier":
        return row.supplier_code
    if label == "lot":
        return row.lot_no
    if row.qty is None:
        return None
    text = f"{fmt_qty(row.qty)} {row.uom or ''}".rstrip()
    if row.qty_base is not None and (row.qty_base != row.qty or row.base_uom != row.uom):
        text += f" = {fmt_qty(row.qty_base)} {row.base_uom or ''}".rstrip()
    return text


def receipt_lines(view: GroupView) -> list[ReceiptLine]:
    """The four field lines of a group in the "All receipts" table: each source's values
    (several rows joined by `, `), the cells of a diff's outlier (with no outlier, every
    non-empty cell) marked as differing, and the outlier named."""
    out: list[ReceiptLine] = []
    for label, fields in _FIELD_ROWS:
        diffs = [d for d in view.group.diffs if d.field in fields]
        cells: dict[Source, ReceiptCell] = {}
        for source in SOURCE_ORDER:
            values = [v for r in view.rows.get(source, []) if (v := _row_value(r, label))]
            odd = any(d.outlier is source or d.outlier is None for d in diffs)
            cells[source] = ReceiptCell(", ".join(values), odd and bool(values))
        named = [SOURCE_TITLE[d.outlier] if d.outlier else "no outlier" for d in diffs]
        out.append(ReceiptLine(label, cells, ", ".join(dict.fromkeys(named))))
    return out


def _label(value: str) -> str:
    return value.replace("_", " ")


@lru_cache(maxsize=1)
def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters.update(day=fmt_date, qty=fmt_qty, instant=fmt_instant, label=_label)
    env.globals.update(
        SOURCES=SOURCE_ORDER,
        SOURCE_TITLE=SOURCE_TITLE,
        CATEGORY_TITLE=CATEGORY_TITLE,
        receipt_lines=receipt_lines,
    )
    return env


def render_report_html(ctx: ReportContext) -> bytes:
    """`report.html` (contracts/frozen-files.md): self-contained UTF-8, sections 1 to 8;
    deterministic for the same context."""
    template = _environment().get_template("report.html.j2")
    return template.render(ctx=ctx).encode("utf-8")


def render_outcomes_csv(rows: Sequence[CanonicalRow], groups: Sequence[Group]) -> bytes:
    """`outcomes.csv` (F-09.R4, research R10): header, then one line per row in `rows` order
    (`list_source_rows`); receipts carry their group's outcome, key, diff fields and outliers."""
    of_row = {row_id: g for g in groups for ids in g.row_ids.values() for row_id in ids}
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(CSV_HEADER)
    for row in rows:
        group = of_row.get(row.source_row_id)
        outcome = group.outcome if group else row.outcome
        diffs = group.diffs if group else []
        writer.writerow(
            [
                row.source.value,
                row.source_row_id,
                "" if row.grn_no is None else row.grn_no,
                outcome.value if outcome else row.row_kind.value,
                group.group_key if group else "",
                ";".join(d.field.value for d in diffs),
                ";".join(d.outlier.value if d.outlier else "" for d in diffs),
                row.row_kind.value,
            ]
        )
    return buffer.getvalue().encode("utf-8")
