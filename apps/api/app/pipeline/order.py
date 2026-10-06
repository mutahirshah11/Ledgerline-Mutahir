"""Shared pipeline orderings (contracts/normalise.md N11, F-05.R11, F-06 C2).

One definition of source order, file order and flag order for normalise, reconcile, controls and
the engine, so pack output and F-05.AC8 determinism cannot drift between stages.
"""

import re

from app.domain.enums import Source
from app.domain.models import CanonicalRow, ControlFlag

SOURCE_ORDER: tuple[Source, ...] = (Source.REGISTER, Source.ERP, Source.SHEET)
"""REGISTER, ERP, SHEET: the order rows, groups' sources and counts are listed in."""

_DIGITS = re.compile(r"\d+")


def file_order(row: CanonicalRow) -> tuple[int, tuple[int, ...]]:
    """Source order, then the numbers in the row id (REGISTER page and row, ERP line, SHEET
    row), so `XLS-r9` sorts before `XLS-r10` (N11, F-05.R11)."""
    numbers = tuple(int(n) for n in _DIGITS.findall(row.source_row_id))
    return SOURCE_ORDER.index(row.source), numbers


def flag_order(flag: ControlFlag) -> tuple[str, bool, int, str]:
    """Flag type, then GRN (nulls last), then row id (F-06 C2), for F-04 and F-06 flags."""
    return (flag.flag.value, flag.grn_no is None, flag.grn_no or 0, flag.source_row_id or "")
