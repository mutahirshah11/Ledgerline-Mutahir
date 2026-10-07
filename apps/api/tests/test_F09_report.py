"""F-09 exception list, line templates and headline (005 US2: T032 to T034; FR-017 to FR-020,
C5; contracts/telegram-message.md "Line templates"); frozen report files and EP-18 (US3: T040
to T043; FR-022 to FR-024, contracts/frozen-files.md)."""

import random
import re
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core import clock
from app.core.errors import UniqueViolation
from app.core.storage import REPORTS_BUCKET, BlobStore, MemoryBlobStore
from app.domain.enums import (
    CellStatus,
    ComparedField,
    FlagType,
    Outcome,
    ReadVariant,
    ReviewKind,
    ReviewReason,
    RowKind,
    RunStatus,
    Source,
)
from app.domain.models import (
    CanonicalRow,
    Cell,
    ControlFlag,
    Diff,
    ExceptionItem,
    Group,
    PageLabelMismatchContext,
    ReportLinks,
    ReviewItem,
    RowMeta,
    UnknownMappingContext,
)
from app.pipeline import report
from app.services import finalise
from tests.factories import T0, make_flag, make_group, make_review, make_row, make_run
from tests.support.pack_run import CONTROLLER, PackRun
from tests.support.trace import assert_traced, numbers_in

API = "/api/v1"


def _items(pack_run: PackRun) -> list[ExceptionItem]:
    repo, run_id = pack_run.repo, pack_run.run_id
    return report.exception_list(
        repo.get_run(run_id),
        repo.list_groups(run_id),
        repo.list_source_rows(run_id),
        repo.list_flags(run_id),
        repo.list_review_items(run_id),
        repo.get_proof(run_id),
    )


def _label(item: ExceptionItem) -> int | str | None:
    """GRN for GRN-level items, the row id for row-level ones (duplicates, returns)."""
    return item.source_row_id if item.kind in ("DUPLICATE", "NON_RECEIPT") else item.grn_no


def _by_label(items: list[ExceptionItem]) -> dict[int | str | None, ExceptionItem]:
    return {_label(i): i for i in items}


# --- T032 ----------------------------------------------------------------------------------


def test_F09_AC1_first_exceptions_4421_4430_4417(
    pack_run: PackRun, api_client_as: Callable[[str], TestClient]
) -> None:
    first = _items(pack_run)[:3]
    assert [(i.grn_no, i.kind) for i in first] == [
        (4421, "MISSING"),
        (4430, "MISSING"),
        (4417, "MISMATCH"),
    ]
    assert first[2].field == "qty"
    body = api_client_as("reviewer").get(f"{API}/runs/{pack_run.run_id}").json()
    assert [(e["grn_no"], e["kind"]) for e in body["exceptions"][:3]] == [
        (4421, "MISSING"),
        (4430, "MISSING"),
        (4417, "MISMATCH"),
    ]


# --- T033 ----------------------------------------------------------------------------------


def test_F09_R1_overview_headline_and_counts(
    pack_run: PackRun, api_client_as: Callable[[str], TestClient]
) -> None:
    resp = api_client_as("reviewer").get(f"{API}/runs/{pack_run.run_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["headline"] == "29 receipts: 21 match, 8 need attention"
    assert {k: v for k, v in body["counts"].items() if v} == {
        "MATCHED": 21,
        "MISMATCHED": 4,
        "MISSING": 2,
        "NEEDS_REVIEW": 2,
        "DUPLICATE": 1,
        "NON_RECEIPT": 1,
    }
    assert body["proof_status"] == "PASS"
    assert body["open_review_items"] == 4  # 2 CELL + 2 FLAG_ACK (engine.open_review_count)
    assert body["row_counts"]["SHEET"]["DUPLICATE"] == 1
    assert sum(sum(by.values()) for by in body["row_counts"].values()) == 86
    assert report.headline(pack_run.repo.list_groups(pack_run.run_id)) == body["headline"]


# --- T034 ----------------------------------------------------------------------------------


def test_F09_R1_category_order_and_ties(pack_run: PackRun) -> None:
    items = _items(pack_run)
    assert [_label(i) for i in items] == [
        4421, 4430, 4417, 4425, 4412, 4415, 4428, 4419, 4429, "XLS-r11", "XLS-r31"
    ]  # fmt: skip
    assert [i.category for i in items] == [1, 1, 2, 2, 2, 2, 3, 3, 4, 5, 5]
    assert [i.kind for i in items] == [
        "MISSING", "MISSING", "MISMATCH", "MISMATCH", "MISMATCH", "MISMATCH",
        "REVIEW", "REVIEW", "FLAG", "DUPLICATE", "NON_RECEIPT",
    ]  # fmt: skip
    assert [i.field for i in items[2:6]] == ["qty", "qty", "lot_no", "date"]
    # deterministic: input order never changes the list (pure function, one sort key)
    repo, run_id = pack_run.repo, pack_run.run_id
    rng = random.Random(9)
    groups = rng.sample(repo.list_groups(run_id), 29)
    rows = list(reversed(repo.list_source_rows(run_id)))
    flags = list(reversed(repo.list_flags(run_id)))
    review = list(reversed(repo.list_review_items(run_id)))
    again = report.exception_list(
        repo.get_run(run_id), groups, rows, flags, review, repo.get_proof(run_id)
    )
    assert again == items


def test_F09_C5_one_line_per_grn_flags_attached(pack_run: PackRun) -> None:
    items = _items(pack_run)
    assert [i.grn_no for i in items].count(4430) == 1
    by = _by_label(items)
    assert by[4430].category == 1
    assert [f.flag.value for f in by[4430].flags] == ["BACKDATED_ENTRY", "UNUSUAL_USER"]
    assert "dated 29 Sep, keyed 2 Oct by admin2" in by[4430].text
    assert [f.flag.value for f in by[4415].flags] == ["SUNDAY_DATE"]
    assert "13 Sep (Sun)" in by[4415].text
    assert [(f.flag.value, f.source_row_id) for f in by["XLS-r31"].flags] == [
        ("SUNDAY_DATE", "XLS-r31"),
        ("OUT_OF_ORDER", "XLS-r31"),
    ]
    assert [f.flag.value for f in by[4429].flags] == ["SEQUENCE_GAP"]
    # every flag appears exactly once across the list
    shown = sorted(str(f.id) for i in items for f in i.flags)
    assert shown == sorted(str(f.id) for f in pack_run.repo.list_flags(pack_run.run_id))


def test_F09_R2_line_templates_exact(pack_run: PackRun) -> None:
    items = _items(pack_run)
    assert [i.text for i in items] == [
        "4421 stretch film, 75 ROLL: in register + sheet, NOT in ERP",
        "4430 cartons 5-ply, 1900 PCS: ERP only, dated 29 Sep, keyed 2 Oct by admin2",
        "4417 qty: ERP 46 CTN, register + sheet 48 CTN",
        "4425 qty: sheet 1200 M, register 1200 YD = ERP 1097.28 M",
        "4412 lot: ERP L2609-021, register + sheet L2609-012",
        "4415 date: ERP 13 Sep (Sun), register + sheet 12 Sep",
        "4428 qty unreadable on register (ink blot)",
        "4419 qty corrected on register, 60 to 66",
        "GRN 4429 missing everywhere",
        "Sheet lists 4409 twice",
        "Sheet lists one return (-5000 PCS)",
    ]
    assert report.also_phrases(items) == [
        "GRN 4429 missing everywhere",
        "Sheet lists 4409 twice and one return (-5000 PCS)",
    ]
    assert report.also_line(items) == (
        "Also: GRN 4429 missing everywhere. Sheet lists 4409 twice and one return (-5000 PCS)."
    )


def test_F09_R2_line_diff_after_review_and_without_outlier(pack_run: PackRun) -> None:
    """The second golden message's 4419 line (register accepted at 66), and the no-outlier and
    several-diffs templates, on the stored 4419 group (quantities without trailing zeros)."""
    repo, run_id = pack_run.repo, pack_run.run_id
    group = repo.get_group(run_id, "GRN:4419")
    qty = group.diffs[0]
    accepted = qty.model_copy(
        update={"values": {**qty.values, Source.REGISTER: "66 DRUM"}, "outlier": Source.SHEET}
    )
    mismatched = group.model_copy(update={"outcome": Outcome.MISMATCHED, "diffs": [accepted]})
    assert report.line_diff(mismatched, []) == "4419 qty: sheet 60 DRUM, register + ERP 66 DRUM"
    no_outlier = qty.model_copy(
        update={"values": {**qty.values, Source.REGISTER: "70.00 DRUM"}, "outlier": None}
    )
    lot = qty.model_copy(
        update={
            "field": ComparedField.LOT_NO,
            "values": {Source.REGISTER: "A", Source.ERP: "B", Source.SHEET: "B"},
            "outlier": Source.REGISTER,
        }
    )
    two = mismatched.model_copy(update={"diffs": [no_outlier, lot]})
    assert report.line_diff(two, []) == (
        "4419 qty: register 70 DRUM, ERP 66 DRUM, sheet 60 DRUM; lot: register A, ERP + sheet B"
    )
    one_null = mismatched.model_copy(update={"diffs": [qty]})  # register qty unread: no part
    assert report.line_diff(one_null, []) == "4419 qty: ERP 66 DRUM, sheet 60 DRUM"


# --- US2 review fixes: synthetic inputs (C1, I1 to I3, I6, I7) ------------------------------

RUN = make_run()
RID = RUN.id


def _row(row_id: str, grn: int | None = None, **over: Any) -> CanonicalRow:
    return make_row(RID, row_id, grn_no=grn, **over)


def _flag(flag: FlagType, grn: int | None, row_id: str | None, **detail: str) -> ControlFlag:
    return make_flag(RID, flag=flag, grn_no=grn, source_row_id=row_id, detail=detail)


def _group(key: str, grn: int | None, outcome: Outcome, **row_ids: list[str]) -> Group:
    ids = {Source[s]: v for s, v in row_ids.items()}
    return make_group(RID, key, grn_no=grn, outcome=outcome, row_ids=ids)


def _list(
    groups: tuple[Group, ...] | list[Group] = (),
    rows: tuple[CanonicalRow, ...] | list[CanonicalRow] = (),
    flags: tuple[ControlFlag, ...] | list[ControlFlag] = (),
    items: tuple[ReviewItem, ...] | list[ReviewItem] = (),
    current_keys: frozenset[str] | None = None,
) -> list[ExceptionItem]:
    return report.exception_list(RUN, groups, rows, flags, items, None, current_keys=current_keys)


def _texts(items: list[ExceptionItem]) -> list[str]:
    return [i.text for i in items]


def _dups_and_returns(n_dups: int, n_returns: int) -> list[CanonicalRow]:
    rows = [_row("XLS-r10", 4409)]
    for k in range(n_dups):
        rows.append(
            _row(f"XLS-r1{k + 1}", 4409, row_kind=RowKind.DUPLICATE, duplicate_of="XLS-r10")
        )
    for k in range(n_returns):
        kind = RowKind.NON_RECEIPT
        rows.append(_row(f"XLS-r3{k}", None, row_kind=kind, qty=Decimal(-5), uom="PCS"))
    return rows


@pytest.mark.parametrize(
    ("n_dups", "n_returns", "phrase"),
    [
        (1, 0, "Sheet lists 4409 twice"),
        (0, 1, "Sheet lists one return (-5 PCS)"),
        (0, 3, "Sheet lists 3 returns"),
        (2, 2, "Sheet lists 4409 3 times and 2 returns"),
    ],
)
def test_F09_R2_also_duplicates_and_returns(n_dups: int, n_returns: int, phrase: str) -> None:
    """C1: no "and 0 returns"; one DUPLICATE item per `duplicate_of` (not one per copy)."""
    items = _list(rows=_dups_and_returns(n_dups, n_returns))
    assert report.also_phrases(items) == [phrase]
    assert sum(1 for i in items if i.kind == "DUPLICATE") == min(n_dups, 1)


def _unreadable(key: str, row_id: str, grn: int, note: str | None) -> ReviewItem:
    read = Cell(value=None, status=CellStatus.ILLEGIBLE, note=note)
    return make_review(RID, key, grn_no=grn, source_row_id=row_id, read_a=read)


@pytest.mark.parametrize(
    ("note", "text"),
    [
        ("ink blot over the digits", "4428 qty unreadable on register (ink blot)"),
        ("Stained corner; maybe 25", "4428 qty unreadable on register (stained)"),
        ("page torn, smudge too", "4428 qty unreadable on register (torn)"),
        ("stainless digits look like 25", "4428 qty unreadable on register"),
        ("blot over digits", "4428 qty unreadable on register"),
        ("the model is unsure; 25 or 26", "4428 qty unreadable on register"),
        (None, "4428 qty unreadable on register"),
    ],
)
def test_F09_R2_illegible_cause_closed_vocabulary(note: str | None, text: str) -> None:
    """I1: whole words of a closed vocabulary only; never a clause of the model's note."""
    item = _unreadable("CELL:REG-p1-r01:qty", "REG-p1-r01", 4428, note)
    assert report.line_review(item, {}) == text


def test_F09_C5_category4_absorbs_only_unusual_user_of_backdated_row() -> None:
    """I2: BACKDATED + DATE_OUT_OF_PERIOD on one row without a numbered line are two items."""
    row = "ERP-0040"
    flags = [
        _flag(
            FlagType.BACKDATED_ENTRY,
            4440,
            row,
            grn_date="2026-08-29",
            entered_at="2026-10-02 08:00",
        ),
        _flag(FlagType.UNUSUAL_USER, 4440, row, entered_by="admin2", usual="clerk"),
        _flag(FlagType.DATE_OUT_OF_PERIOD, 4440, row, date="2026-08-29", month="2026-09"),
    ]
    items = _list(rows=[_row(row, 4440)], flags=flags)
    assert _texts(items) == [
        "GRN 4440 keyed 2 Oct by admin2, dated 29 Aug",
        "ERP-0040 dated 29 Aug, outside Sep 2026",
    ]
    assert [[f.flag.value for f in i.flags] for i in items] == [
        ["BACKDATED_ENTRY", "UNUSUAL_USER"],
        ["DATE_OUT_OF_PERIOD"],
    ]


def _mapping(key: str, row_id: str) -> ReviewItem:
    return make_review(
        RID,
        key,
        kind=ReviewKind.UNKNOWN_MAPPING,
        reason=ReviewReason.UNKNOWN_SUPPLIER,
        grn_no=4450,
        source_row_id=row_id,
        field="supplier_code",
        read_a=None,
        read_b=None,
        context=UnknownMappingContext(source=Source.ERP, raw="ACME"),
    )


def test_F09_R1_category3_follows_open_item_rule() -> None:
    """I3: a non-CELL item the last reconcile did not produce (stale) is not live; CELL items
    always are; `current_keys=None` keeps every non-RESOLVED item live."""
    group = _group("GRN:4450", 4450, Outcome.NEEDS_REVIEW, ERP=["ERP-0050"])
    stale = _mapping("MAP:ERP-0050:supplier_code", "ERP-0050")
    rows = [_row("ERP-0050", 4450)]
    live = _list([group], rows, items=[stale])
    assert _texts(live) == ["4450 supplier_code not recognised on ERP (ACME)"]
    none_current: frozenset[str] = frozenset()
    gone = _list([group], rows, items=[stale], current_keys=none_current)
    assert _texts(gone) == ["4450 needs review"]
    ctx = report.build_report_context(
        RUN, [group], rows, [], [stale], None, [], [], [], current_keys=none_current
    )
    assert ctx.exceptions == gone
    assert _list([group], rows, items=[stale], current_keys=frozenset({stale.item_key})) == live
    cell_item = _unreadable("CELL:ERP-0050:qty", "ERP-0050", 4450, "faded")
    both = _list([group], rows, items=[stale, cell_item], current_keys=none_current)
    assert _texts(both) == ["4450 qty unreadable on register (faded)"]


def _page_item() -> ReviewItem:
    ctx = PageLabelMismatchContext(
        page_no=3,
        run_month="2026-09",
        month_written={ReadVariant.A: "Aug", ReadVariant.B: "Aug"},
        page_no_written={ReadVariant.A: "3", ReadVariant.B: "3"},
    )
    return make_review(
        RID,
        "PAGE:3",
        reason=ReviewReason.PAGE_LABEL_MISMATCH,
        source_row_id=None,
        field="page",
        read_a=None,
        read_b=None,
        context=ctx,
    )


def _alone(flag: FlagType, **detail: str) -> tuple[list[CanonicalRow], list[ControlFlag]]:
    return [_row("XLS-r70", 4470)], [_flag(flag, 4470, "XLS-r70", **detail)]


_FLAG_LINES = [
    (FlagType.SUNDAY_DATE, {"date": "2026-09-13"}, "XLS-r70 dated 13 Sep (Sun)"),
    (
        FlagType.OUT_OF_ORDER,
        {"date": "2026-09-02", "previous_row": "XLS-r69", "previous_date": "2026-09-05"},
        "XLS-r70 dated 2 Sep, after XLS-r69 dated 5 Sep",
    ),
    (
        FlagType.UNUSUAL_USER,
        {"entered_by": "admin2", "usual": "clerk"},
        "XLS-r70 keyed by admin2, usually clerk",
    ),
    (FlagType.UNKNOWN_SUPPLIER, {"raw": "ACME"}, "XLS-r70 supplier not on the map (ACME)"),
    (FlagType.UNKNOWN_UNIT, {"raw": "bx"}, "XLS-r70 unit not on the map (bx)"),
]


@pytest.mark.parametrize(("flag", "detail", "text"), _FLAG_LINES)
def test_F09_R2_category5_flag_lines(flag: FlagType, detail: dict[str, str], text: str) -> None:
    """I6: standalone category 5 flags (SCR-04 and the report; not in the message)."""
    rows, flags = _alone(flag, **detail)
    assert [(i.category, i.kind, i.text) for i in _list(rows=rows, flags=flags)] == [
        (5, "FLAG", text)
    ]


def test_F09_R2_templates_beyond_the_pack() -> None:
    """I6: multi-GRN gap, "needs review" fallback, page-level PAGE_LABEL_MISMATCH item; ranks
    of the page item (2) and UNKNOWN_MAPPING (3) in category 3."""
    gap = [_flag(FlagType.SEQUENCE_GAP, 4401, None, low="4401", high="4403")]
    assert _texts(_list(flags=gap)) == ["GRNs 4401 to 4403 missing everywhere"]
    bare = _group("GRN:4460", 4460, Outcome.NEEDS_REVIEW, SHEET=["XLS-r60"])
    assert _texts(_list([bare], [_row("XLS-r60", 4460)])) == ["4460 needs review"]
    group = _group("GRN:4450", 4450, Outcome.NEEDS_REVIEW, ERP=["ERP-0050"])
    rows = [_row("ERP-0050", 4450)]
    items = _list(
        [group], rows, items=[_mapping("MAP:ERP-0050:supplier_code", "ERP-0050"), _page_item()]
    )
    assert [(i.category, i.kind, i.grn_no, i.source_row_id, i.field, i.text) for i in items] == [
        (3, "REVIEW", None, None, "page", "Register page 3: label does not match the run"),
        (
            3,
            "REVIEW",
            4450,
            None,
            "supplier_code",
            "4450 supplier_code not recognised on ERP (ACME)",
        ),
    ]


def test_F09_C5_ack_clause_on_diff_and_review_lines() -> None:
    """I7: an attached BACKDATED_ENTRY / DATE_OUT_OF_PERIOD is printed on category 2 and 3
    lines too."""
    values: dict[Source, str | None] = {Source.REGISTER: "A", Source.ERP: "B", Source.SHEET: "A"}
    diff = Diff(field=ComparedField.LOT_NO, values=values, compared=values, outlier=Source.ERP)
    mism = _group("GRN:4480", 4480, Outcome.MISMATCHED, ERP=["ERP-0080"])
    mism = mism.model_copy(update={"diffs": [diff]})
    review = _group("GRN:4481", 4481, Outcome.NEEDS_REVIEW, ERP=["ERP-0081"])
    rows = [_row("ERP-0080", 4480), _row("ERP-0081", 4481, meta=RowMeta(entered_by="clerk"))]
    flags = [
        _flag(
            FlagType.BACKDATED_ENTRY,
            4480,
            "ERP-0080",
            grn_date="2026-09-01",
            entered_at="2026-10-02 08:00",
        ),
        _flag(FlagType.DATE_OUT_OF_PERIOD, 4481, "ERP-0081", date="2026-08-30", month="2026-09"),
    ]
    assert _texts(_list([mism, review], rows, flags)) == [
        "4480 lot: ERP B, register + sheet A, dated 1 Sep, keyed 2 Oct",
        "4481 needs review, dated 30 Aug, outside Sep 2026",
    ]


def test_F09_C5_shared_grn_attachment_ignores_group_order() -> None:
    """Groups are sorted by (GRN, group key) before flags attach to them."""
    a = _group("GRN:4490", 4490, Outcome.NEEDS_REVIEW, ERP=["ERP-0090"])
    b = _group("GRN:4490|2", 4490, Outcome.NEEDS_REVIEW, SHEET=["XLS-r90"])
    rows = [_row("ERP-0090", 4490), _row("XLS-r90", 4490)]
    gap = [_flag(FlagType.SEQUENCE_GAP, 4490, None, low="4490", high="4490")]
    first = _list([a, b], rows, gap)
    assert _list([b, a], rows, gap) == first
    assert [i.group_key for i in first if i.flags] == ["GRN:4490"]


# --- US3: frozen report files (T040 to T043; F-09.R3 to R5, contracts/frozen-files.md) -------


def _finalise(pack_run: PackRun, store: BlobStore | None = None) -> tuple[str, str]:
    """US4's finalise order (research R8): one instant, report files first, then FINALISED."""
    repo, run_id = pack_run.repo, pack_run.run_id
    at = clock.now()
    paths = finalise.write_report_files(repo, store or pack_run.blobs, run_id, at, CONTROLLER.email)
    if repo.get_run(run_id).status is not RunStatus.FINALISED:
        repo.transition_run(
            run_id,
            RunStatus.FINALISED,
            expected={RunStatus.AWAITING_REVIEW, RunStatus.READY},
            finalised_at=at,
            finalised_by=CONTROLLER.email,
        )
    return paths


def _report(pack_run: PackRun) -> tuple[str, str]:
    html_path, csv_path = _finalise(pack_run)
    get = pack_run.blobs.get
    return get(REPORTS_BUCKET, html_path).decode(), get(REPORTS_BUCKET, csv_path).decode()


_OPAQUE = re.compile(r'<(\w+)[^>]*data-trace="opaque"[^>]*>.*?</\1>', re.DOTALL)
SECTIONS = [
    "header", "proof", "exceptions", "receipts", "flags", "decisions", "normalisation", "inputs"
]  # fmt: skip


def test_F09_AC3_outcomes_csv_86_lines(pack_run: PackRun) -> None:
    _, csv_text = _report(pack_run)
    assert csv_text.endswith("\r\n")
    lines = csv_text.split("\r\n")[:-1]
    assert len(lines) == 1 + 86
    assert lines[0] == "source,source_row_id,grn_no,outcome,group_key,diff_fields,outlier,row_kind"
    for example in (
        "REGISTER,REG-p2-r11,4425,MISMATCHED,GRN:4425,qty,SHEET,RECEIPT",
        "SHEET,XLS-r11,4409,DUPLICATE,,,,DUPLICATE",
        "SHEET,XLS-r31,,NON_RECEIPT,,,,NON_RECEIPT",
    ):
        assert example in lines
    rows = pack_run.repo.list_source_rows(pack_run.run_id)
    assert [line.split(",")[1] for line in lines[1:]] == [r.source_row_id for r in rows]


def test_F09_AC4_report_offline_with_hashes(pack_run: PackRun) -> None:
    html, _ = _report(pack_run)
    lowered = html.lower()
    assert "<script" not in lowered and "<link" not in lowered and "@import" not in lowered
    assert " src=" not in lowered and "<style>" in lowered and "url(" not in lowered
    outside = _OPAQUE.sub("", html)
    assert "http://" not in outside and "https://" not in outside
    files = pack_run.repo.list_source_files(pack_run.run_id)
    assert len(files) == 4
    for f in files:
        assert f'data-trace="opaque">{f.sha256}<' in html
        assert f.filename in html
    found = [html.find(f'<section id="{s}"') for s in SECTIONS]
    assert -1 not in found and found == sorted(found)
    assert html.count("<section ") == 8
    assert f'data-trace="opaque">{pack_run.run_id}<' in html
    assert "Ledgerline receiving check: Sep 2026" in html
    assert "29 receipts: 21 match, 8 need attention" in html


def test_F09_AC2_report_and_overview_numbers_traced(
    pack_run: PackRun, api_client_as: Callable[[str], TestClient]
) -> None:
    repo, run_id = pack_run.repo, pack_run.run_id
    (item,) = [i for i in repo.list_review_items(run_id) if i.item_key == "CELL:REG-p2-r05:qty"]
    resolve = f"{API}/review-items/{item.id}/resolve"
    decided = api_client_as("reviewer").post(resolve, json={"action": "ACCEPT_READ"})
    assert decided.status_code == 200, decided.text  # a decision row is printed too
    html, _ = _report(pack_run)
    assert "<td>ACCEPT READ</td>" in html
    assert numbers_in(html)  # the check sees the report's numbers
    assert_traced(html, repo, run_id)
    body = api_client_as("reviewer").get(f"{API}/runs/{run_id}").json()
    overview = "\n".join([body["headline"], *(e["text"] for e in body["exceptions"])])
    assert_traced(overview, repo, run_id)


def test_F09_R3_report_labels_casing_and_stable_detail(pack_run: PackRun) -> None:
    """Flag names labelled as elsewhere, one casing for check statuses, flag detail in key
    order (jsonb reorders keys on Postgres)."""
    html, _ = _report(pack_run)
    assert "(flags: BACKDATED ENTRY, UNUSUAL USER)" in html
    assert "_ENTRY" not in html.split('<section id="exceptions"')[1].split("</section>")[0]
    assert "): PASS</h3>" in html and "): pass</h3>" not in html
    backdated = next(line for line in html.splitlines() if "<td>BACKDATED ENTRY</td>" in line)
    assert backdated.index("days_after:") < backdated.index("entered_at:")
    assert backdated.index("entered_at:") < backdated.index("grn_date:")


def test_F09_R3_receipt_lines_missing_and_unread() -> None:
    """ "All receipts": `missing` for a source in `missing_from`, `unread` for an empty qty."""
    group = make_group(
        RID,
        "GRN:4495",
        grn_no=4495,
        outcome=Outcome.MISSING,
        row_ids={Source.REGISTER: ["REG-p1-r01"], Source.SHEET: ["XLS-r95"]},
        missing_from=[Source.ERP],
    )
    rows = [_row("REG-p1-r01", 4495, qty=None), _row("XLS-r95", 4495)]
    (view,) = report.group_views([group], rows, [])
    qty = next(line for line in report.receipt_lines(view) if line.label == "qty")
    texts = {s: c.text for s, c in qty.cells.items()}
    assert texts == {Source.REGISTER: "unread", Source.ERP: "missing", Source.SHEET: "1.5 KG"}
    lot = next(line for line in report.receipt_lines(view) if line.label == "lot")
    assert lot.cells[Source.ERP].text == "missing" and lot.cells[Source.SHEET].text == ""


def test_F09_R5_report_files_write_once(pack_run: PackRun) -> None:
    repo, run_id, blobs = pack_run.repo, pack_run.run_id, pack_run.blobs
    html_path, csv_path = _finalise(pack_run)
    assert (html_path, csv_path) == (f"{run_id}/report.html", f"{run_id}/outcomes.csv")
    first = blobs.get(REPORTS_BUCKET, html_path), blobs.get(REPORTS_BUCKET, csv_path)
    assert _finalise(pack_run) == (html_path, csv_path)  # resend: reuse, write nothing
    assert (blobs.get(REPORTS_BUCKET, html_path), blobs.get(REPORTS_BUCKET, csv_path)) == first
    with pytest.raises(UniqueViolation):
        blobs.put(REPORTS_BUCKET, html_path, b"changed", "text/html")
    # deterministic bytes from the same frozen run
    other = MemoryBlobStore()
    at = repo.get_run(run_id).finalised_at
    assert at is not None
    finalise.write_report_files(repo, other, run_id, at, CONTROLLER.email)
    assert (other.get(REPORTS_BUCKET, html_path), other.get(REPORTS_BUCKET, csv_path)) == first
    # crash between writes: only the missing file is written, the existing one is kept
    partial = MemoryBlobStore()
    partial.put(REPORTS_BUCKET, csv_path, b"kept", "text/csv")
    finalise.write_report_files(repo, partial, run_id, at, CONTROLLER.email)
    assert partial.get(REPORTS_BUCKET, html_path) == first[0]
    assert partial.get(REPORTS_BUCKET, csv_path) == b"kept"


def test_F09_EP18_links_409_before_finalise_signed_after(
    pack_run: PackRun, api_client_as: Callable[[str], TestClient]
) -> None:
    client, run_id = api_client_as("reviewer"), pack_run.run_id
    before = client.get(f"{API}/runs/{run_id}/report")
    assert before.status_code == 409
    assert before.json()["error"]["code"] == "RUN_STATE_CONFLICT"
    html_path, csv_path = _finalise(pack_run)
    resp = client.get(f"{API}/runs/{run_id}/report")
    assert resp.status_code == 200
    links = ReportLinks.model_validate(resp.json())
    assert html_path in links.html_url and csv_path in links.csv_url
    assert links.expires_at == T0 + timedelta(seconds=300)
    assert client.get(f"{API}/runs/{uuid4()}/report").status_code == 404
