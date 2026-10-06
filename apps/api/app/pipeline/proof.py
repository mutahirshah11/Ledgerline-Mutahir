"""F-07 completeness proof (contracts/proof.md; blueprint Section 19, C23). Pure.

Independent of the main parsers (F-07.R3): this module never calls `read_erp`, `read_sheet` or
`normalise_all`. Row counts and quantity totals are taken from the raw bytes with its own minimal
readers, and the register side from the merged reads with `RESOLVED` overrides applied.
"""

import csv
import io
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING
from uuid import UUID

import openpyxl

from app.domain.enums import (
    PRINTED_ROW_SLOTS,
    CellStatus,
    FlagType,
    Outcome,
    ProofStatus,
    RegisterField,
    ReviewKind,
    ReviewReason,
    ReviewStatus,
    RowKind,
    Source,
)
from app.domain.ids import erp_row_id, register_row_id, review_item_key, sheet_row_id
from app.domain.models import (
    CanonicalRow,
    CheckC1,
    CheckC2,
    CheckC3,
    CheckC4,
    CheckC5,
    ControlFlag,
    Group,
    MergedRow,
    Proof,
    ProofChecks,
    QtyBalance,
    RegisterPageInput,
    ReviewItem,
    RowInOneReadContext,
    SourceCount,
)

if TYPE_CHECKING:
    from app.pipeline.engine import EngineInputs

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_SHEET_QTY, _SHEET_UNIT = "Qty Received", "UOM"
_ERP_QTY, _ERP_UNIT = "qty", "uom"
# Raw key holding the unit as recorded, per source (canonical rows keep the source's raw values).
_RAW_UNIT_KEY = {
    Source.REGISTER: RegisterField.UNIT.value,
    Source.ERP: _ERP_UNIT,
    Source.SHEET: _SHEET_UNIT,
}


# --- Minimal raw readers (research R9, R10) -------------------------------------------------


def _blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _text(value: object) -> str | None:
    """Raw text; openpyxl floats via `str` (shortest round-trip), never summed as float."""
    if _blank(value):
        return None
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value).strip()


def _decimal(value: object) -> Decimal | None:
    """The qty as a finite Decimal, else None: blank or non-numeric qty is `qty_unknown`,
    never summed (F-07.R4; research R10)."""
    text = _text(value)
    if text is None:
        return None
    try:
        qty = Decimal(text)
    except InvalidOperation:
        return None
    return qty if qty.is_finite() else None


def _sheet_cells(data: bytes, month: str) -> list[tuple[object, ...]]:
    """All rows of worksheet `Mon-YYYY` (`2026-09` → `Sep-2026`), by this module's own pass."""
    year, mon = month.split("-")
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        return [tuple(r) for r in wb[f"{_MONTHS[int(mon) - 1]}-{year}"].iter_rows(values_only=True)]
    finally:
        wb.close()


def _data_rows(cells: Sequence[tuple[object, ...]]) -> list[tuple[int, tuple[object, ...]]]:
    """(Excel row, values) for rows 2..last row with any non-empty cell."""
    last = max(
        (n for n, r in enumerate(cells, start=1) if n >= 2 and not all(map(_blank, r))),
        default=1,
    )
    return [(n, cells[n - 1]) for n in range(2, last + 1)]


def count_erp_lines(data: bytes) -> int:
    """ERP rows = non-empty physical lines minus one header line (no `csv` module; R9)."""
    lines = [line for line in data.decode("utf-8-sig").splitlines() if line.strip()]
    return max(len(lines) - 1, 0)


def count_sheet_rows(data: bytes, month: str) -> int:
    """SHEET rows = rows 2..last row with any non-empty cell, by a separate openpyxl pass (R9)."""
    return len(_data_rows(_sheet_cells(data, month)))


def _one_read_rows(page: RegisterPageInput) -> list[MergedRow]:
    """Merged rows present in exactly one read (every cell missing from the other read)."""
    out = []
    for row in page.rows:
        cells = list(row.cells.values())
        no_a = all(c.read_a is None for c in cells)
        no_b = all(c.read_b is None for c in cells)
        if no_a != no_b:
            out.append(row)
    return out


def register_counts(pages: Sequence[RegisterPageInput]) -> tuple[int, list[str]]:
    """(Σ merged rows, problems): per page each read's rows + blank slots and the merged rows +
    the fewer blank slots fill the 16 printed slots (F-07.R3, C23; research R9)."""
    total, problems = 0, []
    for page in sorted(pages, key=lambda p: p.page_no):
        p = page.page_no
        total += len(page.rows)
        for read, rows, blank in (
            ("A", page.row_count_a, page.blank_slots_a),
            ("B", page.row_count_b, page.blank_slots_b),
        ):
            if rows + blank != PRINTED_ROW_SLOTS:
                problems.append(f"page {p} read {read}: {rows} rows + {blank} blank != 16")
        blank = min(page.blank_slots_a, page.blank_slots_b)
        if len(page.rows) + blank != PRINTED_ROW_SLOTS:
            problems.append(f"page {p}: {len(page.rows)} merged rows + {blank} blank != 16")
        if page.row_count_a != page.row_count_b and not _one_read_rows(page):
            problems.append(f"page {p}: reads disagree on row count, no one-read row found")
    return total, problems


# --- C1, C2 (F-07.R1, R2) -------------------------------------------------------------------


def check_c1(rows_in: Mapping[Source, int], rows: Sequence[CanonicalRow]) -> CheckC1:
    """Rows produced by F-04 equal the rows with a non-null outcome, per source."""
    placed = Counter(r.source for r in rows if r.outcome is not None)
    per_source = {s: SourceCount(in_=rows_in.get(s, 0), placed=placed[s]) for s in Source}
    return CheckC1(pass_=all(c.in_ == c.placed for c in per_source.values()), per_source=per_source)


def check_c2(rows: Sequence[CanonicalRow], groups: Sequence[Group]) -> CheckC2:
    """Every row has exactly one non-null outcome; no ID twice in the rows or across groups."""
    in_rows = Counter(r.source_row_id for r in rows)
    in_groups = Counter(i for g in groups for ids in g.row_ids.values() for i in ids)
    twice = sorted(
        {i for i, n in in_rows.items() if n > 1} | {i for i, n in in_groups.items() if n > 1}
    )
    unplaced = sorted({r.source_row_id for r in rows if r.outcome is None})
    return CheckC2(
        pass_=not twice and not unplaced, rows=len(rows), unplaced=unplaced, placed_twice=twice
    )


# --- C3 (F-07.R3, C23) ----------------------------------------------------------------------


def _one_read_covered(inputs: "EngineInputs", groups: Sequence[Group]) -> list[str]:
    """Problems for one-read rows without a `ROW_IN_ONE_READ` item, or whose group is not
    `NEEDS_REVIEW` while that item is open (C23)."""
    items: dict[tuple[int, int], ReviewItem] = {}
    for decision in inputs.decisions:
        ctx = decision.context
        if decision.reason is ReviewReason.ROW_IN_ONE_READ and isinstance(ctx, RowInOneReadContext):
            items[(ctx.page_no, ctx.row_index)] = decision
    outcome_of = {i: g.outcome for g in groups for i in g.row_ids.get(Source.REGISTER, [])}
    problems = []
    for page in sorted(inputs.register_pages, key=lambda p: p.page_no):
        for row in _one_read_rows(page):
            row_id = register_row_id(page.page_no, row.row_index)
            item = items.get((page.page_no, row.row_index))
            if item is None:
                problems.append(f"{row_id}: in one read only, no ROW_IN_ONE_READ item")
            elif (
                item.status is ReviewStatus.OPEN
                and outcome_of.get(row_id) is not Outcome.NEEDS_REVIEW
            ):
                problems.append(f"{row_id}: ROW_IN_ONE_READ open but group not NEEDS_REVIEW")
    return problems


def check_c3(
    inputs: "EngineInputs", rows_in: Mapping[Source, int], groups: Sequence[Group]
) -> CheckC3:
    """Independent row counts equal F-04's rows in, per source; register slots add up (R9).
    `problems`: slot and one-read reasons, page order then one-read rows (deterministic)."""
    register, problems = register_counts(inputs.register_pages)
    problems += _one_read_covered(inputs, groups)
    per_source = {
        Source.REGISTER: register,
        Source.ERP: count_erp_lines(inputs.erp_bytes),
        Source.SHEET: count_sheet_rows(inputs.sheet_bytes, inputs.month),
    }
    agree = all(per_source[s] == rows_in.get(s, 0) for s in Source)
    return CheckC3(pass_=agree and not problems, per_source=per_source, problems=problems)


# --- C4 (F-07.R4, P1-B2 / C23; research R10) ------------------------------------------------

_Qty = tuple[str, str, Decimal | None]  # (key, source_row_id, qty)


def _key(source: Source, unit: object) -> str:
    return f"{source.value}|{(_text(unit) or '').upper()}"


def _erp_quantities(data: bytes) -> list[_Qty]:
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline=""))
    out = []
    for line, record in enumerate(reader, start=1):
        row_id = erp_row_id(line)
        out.append(
            (
                _key(Source.ERP, record.get(_ERP_UNIT)),
                row_id,
                _decimal(record.get(_ERP_QTY)),
            )
        )
    return out


def _sheet_quantities(data: bytes, month: str) -> list[_Qty]:
    cells = _sheet_cells(data, month)
    header = [_text(h) for h in (cells[0] if cells else ())]
    qi, ui = header.index(_SHEET_QTY), header.index(_SHEET_UNIT)

    def at(values: tuple[object, ...], i: int) -> object:
        return values[i] if i < len(values) else None

    out = []
    for n, values in _data_rows(cells):
        row_id = sheet_row_id(n)
        qty = at(values, qi)
        if isinstance(qty, int) and not isinstance(qty, bool):
            qty = Decimal(qty)
        out.append((_key(Source.SHEET, at(values, ui)), row_id, _decimal(qty)))
    return out


def _resolved_cells(decisions: Iterable[ReviewItem]) -> dict[str, str]:
    """`RESOLVED` CELL overrides by item key `CELL:{row_id}:{register_field}` (F-04.R2)."""
    return {
        d.item_key: d.resolved_value
        for d in decisions
        if d.kind is ReviewKind.CELL
        and d.status is ReviewStatus.RESOLVED
        and d.resolved_value is not None
    }


def _register_quantities(
    pages: Sequence[RegisterPageInput], decisions: Iterable[ReviewItem]
) -> list[_Qty]:
    """The effective register record: merged qty when `CLEAR`, a `RESOLVED` CELL override
    replacing it; the unit as recorded on the page (C23)."""
    overrides = _resolved_cells(decisions)
    out = []
    for page in sorted(pages, key=lambda p: p.page_no):
        for row in page.rows:
            row_id = register_row_id(page.page_no, row.row_index)
            qty_cell = row.cells.get(RegisterField.QTY)
            unit_cell = row.cells.get(RegisterField.UNIT)
            key = review_item_key(ReviewKind.CELL, row_id=row_id, field=RegisterField.QTY.value)
            if key in overrides:
                raw: str | None = overrides[key]
            elif qty_cell is not None and qty_cell.merged.status is CellStatus.CLEAR:
                raw = qty_cell.merged.value
            else:
                raw = None
            unit = unit_cell.merged.value if unit_cell is not None else None
            out.append((_key(Source.REGISTER, unit), row_id, _decimal(raw)))
    return out


def _sum(quantities: Iterable[_Qty]) -> tuple[dict[str, Decimal], set[str]]:
    totals: dict[str, Decimal] = {}
    unknown = set()
    for key, row_id, qty in quantities:
        if qty is None:
            unknown.add(row_id)
        else:
            totals[key] = totals.get(key, Decimal(0)) + qty
    return totals, unknown


def check_c4(inputs: "EngineInputs", rows: Sequence[CanonicalRow]) -> CheckC4:
    """Σ qty per `SOURCE|UNIT as recorded`: raw side == placed rows; nulls listed, never summed."""
    raw_in, unknown_in = _sum(
        [
            *_register_quantities(inputs.register_pages, inputs.decisions),
            *_erp_quantities(inputs.erp_bytes),
            *_sheet_quantities(inputs.sheet_bytes, inputs.month),
        ]
    )
    placed, unknown_placed = _sum(
        (_key(r.source, r.raw.get(_RAW_UNIT_KEY[r.source])), r.source_row_id, r.qty)
        for r in rows
        if r.outcome is not None
    )
    zero = Decimal(0)
    balance = {
        k: QtyBalance(in_=raw_in.get(k, zero), placed=placed.get(k, zero))
        for k in sorted(raw_in.keys() | placed.keys())
    }
    return CheckC4(
        pass_=all(b.in_ == b.placed for b in balance.values()),
        by_source_unit=balance,
        qty_unknown=sorted(unknown_in | unknown_placed),
    )


# --- C5 (F-07.R5) ---------------------------------------------------------------------------


def check_c5(
    rows: Sequence[CanonicalRow], groups: Sequence[Group], flags: Sequence[ControlFlag]
) -> CheckC5:
    """GRNs of receipt rows == GRNs of groups; every missing number in range has a gap flag."""
    row_grns = {r.grn_no for r in rows if r.row_kind is RowKind.RECEIPT and r.grn_no is not None}
    group_grns = {g.grn_no for g in groups if g.grn_no is not None}
    gaps = (
        sorted(set(range(min(group_grns), max(group_grns) + 1)) - group_grns) if group_grns else []
    )
    flagged = {f.grn_no for f in flags if f.flag is FlagType.SEQUENCE_GAP}
    ok = row_grns == group_grns and all(g in flagged for g in gaps)
    return CheckC5(pass_=ok, grns=len(group_grns), gaps=gaps)


# --- Proof (F-07.R6) ------------------------------------------------------------------------


def prove(
    run_id: UUID,
    inputs: "EngineInputs",
    rows_in: Mapping[Source, int],
    rows: Sequence[CanonicalRow],
    groups: Sequence[Group],
    flags: Sequence[ControlFlag],
    now: datetime,
) -> Proof:
    """The five checks; `PASS` only if all pass; `computed_at = now` (the scenario clock)."""
    checks = ProofChecks(
        C1=check_c1(rows_in, rows),
        C2=check_c2(rows, groups),
        C3=check_c3(inputs, rows_in, groups),
        C4=check_c4(inputs, rows),
        C5=check_c5(rows, groups, flags),
    )
    status = ProofStatus.PASS if checks.all_pass() else ProofStatus.FAIL
    return Proof(run_id=run_id, status=status, checks=checks, computed_at=now)
