"""F-09 exception list, line templates and headline (005 US2: T032 to T034; FR-017 to FR-020,
C5; contracts/telegram-message.md "Line templates")."""

import random
from collections.abc import Callable

from fastapi.testclient import TestClient

from app.domain.enums import ComparedField, Outcome, Source
from app.domain.models import ExceptionItem
from app.pipeline import report
from tests.support.pack_run import PackRun

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
