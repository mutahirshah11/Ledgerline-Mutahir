"""Identifier and key helpers (data-model.md Section 3). Invalid inputs raise ValueError."""

import hashlib
import re
from collections.abc import Iterable
from uuid import UUID

from app.domain.enums import PRINTED_ROW_SLOTS, FlagType, ReviewKind, Stage

_MONTH = re.compile(r"\d{4}-(0[1-9]|1[0-2])")


def _int_at_least(name: str, value: int, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an int >= {minimum}, got {value!r}")
    return value


def _non_empty(name: str, value: str | None) -> str:
    if not value:
        raise ValueError(f"{name} is required")
    return value


def register_row_id(page_no: int, row_index: int) -> str:
    """`REG-p{page_no}-r{row_index:02}`, row_index 1 to 16."""
    _int_at_least("page_no", page_no, 1)
    _int_at_least("row_index", row_index, 1)
    if row_index > PRINTED_ROW_SLOTS:
        raise ValueError(f"row_index must be <= {PRINTED_ROW_SLOTS}, got {row_index}")
    return f"REG-p{page_no}-r{row_index:02}"


def erp_row_id(line_number: int) -> str:
    """`ERP-{line:04}`; line 1 is the first data row."""
    return f"ERP-{_int_at_least('line_number', line_number, 1):04}"


def sheet_row_id(excel_row: int) -> str:
    """`XLS-r{excel_row}`; Excel row 2 is the first data row."""
    return f"XLS-r{_int_at_least('excel_row', excel_row, 2)}"


def grn_group_key(grn_no: int) -> str:
    return f"GRN:{_int_at_least('grn_no', grn_no, 1)}"


def lot_group_key(lot_no: str, supplier_code: str) -> str:
    return f"LOT:{_non_empty('lot_no', lot_no)}|{_non_empty('supplier_code', supplier_code)}"


def review_item_key(
    kind: ReviewKind,
    *,
    row_id: str | None = None,
    field: str | None = None,
    flag: FlagType | None = None,
    grn_no: int | None = None,
) -> str:
    """Stable review item key: CELL:{row_id}:{field}, MAP:{row_id}:{field}, ROW:{row_id},
    FLAG:{flag}:{row_id or grn}."""
    if kind is ReviewKind.CELL:
        return f"CELL:{_non_empty('row_id', row_id)}:{_non_empty('field', field)}"
    if kind is ReviewKind.UNKNOWN_MAPPING:
        return f"MAP:{_non_empty('row_id', row_id)}:{_non_empty('field', field)}"
    if kind is ReviewKind.UNMATCHED_ROW:
        return f"ROW:{_non_empty('row_id', row_id)}"
    if flag is None:
        raise ValueError("flag is required")
    if row_id:
        return f"FLAG:{flag.value}:{row_id}"
    if grn_no is None:
        raise ValueError("row_id or grn_no is required")
    return f"FLAG:{flag.value}:{_int_at_least('grn_no', grn_no, 1)}"


def summary_key(run_key: str) -> str:
    return f"SUMMARY:{_non_empty('run_key', run_key)}"


def alert_key(run_id: UUID | str, stage: Stage, attempt_group: int | str) -> str:
    return f"ALERT:{run_id}:{Stage(stage).value}:{attempt_group}"


def run_key(month: str, file_hashes: Iterable[str]) -> str:
    """sha256(month + "".join(sorted(hashes))) as hex; independent of upload order."""
    if not _MONTH.fullmatch(month):
        raise ValueError(f"month must be YYYY-MM, got {month!r}")
    return hashlib.sha256((month + "".join(sorted(file_hashes))).encode()).hexdigest()
