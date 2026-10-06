"""F-06 rules on hand-built canonical rows (contracts/controls.md, data-model Section 7)."""

import datetime as dt
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.domain.enums import FlagType, ReviewKind, ReviewReason
from app.domain.ids import stable_id
from app.domain.models import CanonicalRow, ControlFlag, FlagAckContext, RowMeta
from app.pipeline.controls import ControlsResult, run_controls
from app.pipeline.reconcile import reconcile
from tests.factories import KARACHI, make_row

RUN = UUID("00000000-0000-4000-8000-0000000000f6")
MONTH = "2026-09"
SEP_1 = dt.date(2026, 9, 1)  # a Tuesday


def _row(row_id: str, grn: int | None = 4401, **over: Any) -> CanonicalRow:
    """A clear receipt row dated 1 Sep 2026; same defaults for every source."""
    base: dict[str, Any] = dict(
        grn_no=grn,
        date=SEP_1,
        supplier_code="S-101",
        item="Polybags",
        lot_no="L2609-001",
        qty=Decimal("10"),
        uom="KG",
        qty_base=Decimal("10.00"),
        base_uom="KG",
    )
    return make_row(RUN, row_id, **(base | over))


def _erp(
    line: int,
    grn: int,
    date: dt.date | None = SEP_1,
    entered: dt.datetime | None = None,
    by: str | None = "store.clerk",
    **over: Any,
) -> CanonicalRow:
    if entered is None and date is not None:
        entered = dt.datetime.combine(date, dt.time(10, 0), tzinfo=KARACHI)
    meta = RowMeta(entered_by=by, entered_at=entered)
    return _row(f"ERP-{line:04}", grn, date=date, meta=meta, **over)


def _flags(result: ControlsResult, flag: FlagType) -> list[ControlFlag]:
    return [f for f in result.flags if f.flag is flag]


def _flagged_rows(result: ControlsResult, flag: FlagType) -> list[str | None]:
    return [f.source_row_id for f in _flags(result, flag)]


def test_F06_sequence_gap_every_missing_integer() -> None:
    rows = [_row("REG-p1-r01", 4401), _row("ERP-0001", 4404), _row("XLS-r2", None)]
    result = run_controls(RUN, MONTH, rows)

    gaps = _flags(result, FlagType.SEQUENCE_GAP)
    assert [(g.grn_no, g.source_row_id) for g in gaps] == [(4402, None), (4403, None)]
    keys = [i.item_key for i in result.review_items if i.reason is ReviewReason.SEQUENCE_GAP]
    assert keys == ["FLAG:SEQUENCE_GAP:4402", "FLAG:SEQUENCE_GAP:4403"]


def test_F06_date_out_of_period_flag_ack() -> None:
    rows = [
        _row("REG-p1-r01", 4401, date=dt.date(2026, 8, 31)),
        _row("REG-p1-r02", 4402, date=dt.date(2026, 9, 30)),
        _row("XLS-r2", 4401, date=dt.date(2026, 10, 1)),
        _row("XLS-r3", 4402, date=None),
    ]
    result = run_controls(RUN, MONTH, rows)

    assert _flagged_rows(result, FlagType.DATE_OUT_OF_PERIOD) == ["REG-p1-r01", "XLS-r2"]
    items = [i for i in result.review_items if i.reason is ReviewReason.DATE_OUT_OF_PERIOD]
    assert [i.item_key for i in items] == [
        "FLAG:DATE_OUT_OF_PERIOD:REG-p1-r01",
        "FLAG:DATE_OUT_OF_PERIOD:XLS-r2",
    ]
    item = items[0]
    assert item.kind is ReviewKind.FLAG_ACK
    assert (item.grn_no, item.source_row_id, item.field) == (4401, "REG-p1-r01", None)
    assert item.id == stable_id(RUN, item.item_key)
    assert item.context == FlagAckContext(
        flag=FlagType.DATE_OUT_OF_PERIOD, detail={"date": "2026-08-31", "month": "2026-09"}
    )


def test_F06_backdate_threshold_boundary() -> None:
    sep_10 = dt.date(2026, 9, 10)
    rows = [
        _erp(1, 4401, sep_10, dt.datetime(2026, 9, 11, 23, 59, tzinfo=KARACHI)),  # 1 day
        _erp(2, 4402, sep_10, dt.datetime(2026, 9, 12, 0, 1, tzinfo=KARACHI)),  # 2 days
        _erp(3, 4403, dt.date(2026, 9, 30), dt.datetime(2026, 10, 1, 9, 0, tzinfo=KARACHI)),
        # 1 Oct 01:00 in Karachi is still 30 Sep in UTC: the Karachi date decides
        _erp(4, 4404, dt.date(2026, 9, 30), dt.datetime(2026, 9, 30, 20, 0, tzinfo=dt.UTC)),
        _erp(5, 4405, None, dt.datetime(2026, 10, 9, 9, 0, tzinfo=KARACHI)),
        _row("REG-p1-r01", 4406, date=sep_10),
    ]
    result = run_controls(RUN, MONTH, rows)

    assert _flagged_rows(result, FlagType.BACKDATED_ENTRY) == ["ERP-0002", "ERP-0003", "ERP-0004"]
    keys = [i.item_key for i in result.review_items if i.reason is ReviewReason.BACKDATED_ENTRY]
    assert keys == [
        "FLAG:BACKDATED_ENTRY:ERP-0002",
        "FLAG:BACKDATED_ENTRY:ERP-0003",
        "FLAG:BACKDATED_ENTRY:ERP-0004",
    ]


def test_F06_backdate_days_parameter() -> None:
    rows = [_erp(1, 4401, SEP_1, dt.datetime(2026, 9, 3, 9, 0, tzinfo=KARACHI))]
    assert _flagged_rows(run_controls(RUN, MONTH, rows), FlagType.BACKDATED_ENTRY) == ["ERP-0001"]
    assert (
        _flagged_rows(run_controls(RUN, MONTH, rows, backdate_days=2), FlagType.BACKDATED_ENTRY)
        == []
    )


def test_F06_out_of_order_skips_null_and_equal() -> None:
    rows = [
        _row("XLS-r2", 4401, date=dt.date(2026, 9, 5)),
        _row("XLS-r3", 4402, date=None),
        _row("XLS-r4", 4403, date=dt.date(2026, 9, 5)),  # equal: fine
        _row("XLS-r5", 4404, date=None),
        _row("XLS-r6", 4405, date=dt.date(2026, 9, 4)),  # earlier than r4 across a null
        _row("XLS-r10", 4406, date=dt.date(2026, 9, 8)),  # r10 follows r6, not r1
        _erp(9, 4407, dt.date(2026, 9, 2)),
        _erp(10, 4408, dt.date(2026, 9, 1)),
        _row("REG-p1-r01", 4409, date=dt.date(2026, 9, 9)),
        _row("REG-p1-r02", 4410, date=dt.date(2026, 9, 1)),  # register is not checked
    ]
    result = run_controls(RUN, MONTH, list(reversed(rows)))

    flags = _flags(result, FlagType.OUT_OF_ORDER)
    assert [f.source_row_id for f in flags] == ["XLS-r6", "ERP-0010"]
    assert flags[0].detail == {
        "date": "2026-09-04",
        "previous_row": "XLS-r4",
        "previous_date": "2026-09-05",
    }
    assert not any(i.reason.value == FlagType.OUT_OF_ORDER.value for i in result.review_items)


def test_F06_sunday_date_any_row() -> None:
    sunday = dt.date(2026, 9, 13)
    rows = [
        _row("REG-p1-r01", 4401, date=sunday),
        _row("XLS-r2", None, date=sunday, qty=Decimal("-5")),  # non-receipt rows too
        _row("XLS-r3", 4402, date=dt.date(2026, 9, 12)),
        _row("XLS-r4", 4403, date=None),
    ]
    result = run_controls(RUN, MONTH, rows)

    assert _flagged_rows(result, FlagType.SUNDAY_DATE) == ["REG-p1-r01", "XLS-r2"]
    assert result.review_items == []


def test_F06_unusual_user_tie_rule() -> None:
    rows = [
        _erp(1, 4401, by="zara"),
        _erp(2, 4402, by="amir"),
        _erp(3, 4403, by="zara"),
        _erp(4, 4404, by="amir"),
        _erp(5, 4405, by="bilal"),
    ]
    result = run_controls(RUN, MONTH, rows)

    flags = _flags(result, FlagType.UNUSUAL_USER)
    assert [f.source_row_id for f in flags] == ["ERP-0001", "ERP-0003", "ERP-0005"]
    assert flags[0].detail == {"entered_by": "zara", "usual": "amir"}
    assert result.review_items == []


def test_F06_detail_shapes() -> None:
    rows = [
        _row("REG-p1-r01", 4401, date=dt.date(2026, 9, 13)),
        _erp(
            2,
            4401,
            dt.date(2026, 9, 29),
            dt.datetime(2026, 10, 2, 18, 47, tzinfo=KARACHI),
            by="admin2",
        ),
        _erp(3, 4404, dt.date(2026, 9, 29)),
        _erp(4, 4405, dt.date(2026, 9, 29)),
        _row("REG-p1-r02", 4406, date=dt.date(2026, 10, 3)),
        _row("XLS-r2", 4406, date=dt.date(2026, 9, 28)),
        _row("XLS-r3", None, date=dt.date(2026, 9, 27)),
    ]
    result = run_controls(RUN, MONTH, rows)

    shapes = {(f.flag, f.grn_no, f.source_row_id): f.detail for f in result.flags}
    assert shapes == {
        (FlagType.BACKDATED_ENTRY, 4401, "ERP-0002"): {
            "grn_date": "2026-09-29",
            "entered_at": "2026-10-02 18:47",
            "days_after": "3",
        },
        (FlagType.DATE_OUT_OF_PERIOD, 4406, "REG-p1-r02"): {
            "date": "2026-10-03",
            "month": "2026-09",
        },
        (FlagType.OUT_OF_ORDER, None, "XLS-r3"): {
            "date": "2026-09-27",
            "previous_row": "XLS-r2",
            "previous_date": "2026-09-28",
        },
        (FlagType.SEQUENCE_GAP, 4402, None): {"low": "4401", "high": "4406"},
        (FlagType.SEQUENCE_GAP, 4403, None): {"low": "4401", "high": "4406"},
        (FlagType.SUNDAY_DATE, 4401, "REG-p1-r01"): {"date": "2026-09-13"},
        (FlagType.SUNDAY_DATE, None, "XLS-r3"): {"date": "2026-09-27"},
        (FlagType.UNUSUAL_USER, 4401, "ERP-0002"): {"entered_by": "admin2", "usual": "store.clerk"},
    }
    for f in result.flags:
        assert f.run_id == RUN
        assert f.id == stable_id(RUN, f.flag.value, f.source_row_id or str(f.grn_no))
    for item in result.review_items:
        flag = next(
            f
            for f in result.flags
            if f.flag.value == item.reason.value
            and (f.source_row_id or str(f.grn_no)) == item.item_key.split(":", 2)[2]
        )
        assert item.context == FlagAckContext(flag=flag.flag, detail=flag.detail)


def test_F06_output_sorted_and_deterministic() -> None:
    rows = [
        _row("XLS-r3", None, date=dt.date(2026, 9, 13)),
        _row("REG-p1-r01", 4405, date=dt.date(2026, 9, 13)),
        _row("ERP-0001", 4401, date=dt.date(2026, 9, 13)),
    ]
    result = run_controls(RUN, MONTH, rows)

    assert [(f.flag, f.grn_no, f.source_row_id) for f in result.flags] == [
        (FlagType.SEQUENCE_GAP, 4402, None),
        (FlagType.SEQUENCE_GAP, 4403, None),
        (FlagType.SEQUENCE_GAP, 4404, None),
        (FlagType.SUNDAY_DATE, 4401, "ERP-0001"),
        (FlagType.SUNDAY_DATE, 4405, "REG-p1-r01"),
        (FlagType.SUNDAY_DATE, None, "XLS-r3"),
    ]
    assert run_controls(RUN, MONTH, list(reversed(rows))) == result
    keys = [i.item_key for i in result.review_items]
    assert keys == sorted(keys)


def test_F06_not_a_problem_h202() -> None:
    rows = [
        _row("REG-p1-r01", 4401, item="H202"),
        _erp(1, 4401, item="H2O2 35%"),
        _row("XLS-r2", 4401, item="Hydrogen Peroxide"),
    ]
    reconciled = reconcile(RUN, rows)
    (group,) = reconciled.groups
    assert group.diffs == []

    result = run_controls(RUN, MONTH, reconciled.rows)
    assert result.flags == []
    assert result.review_items == []
