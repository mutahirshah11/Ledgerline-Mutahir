"""F-08 human review: EP-14 to EP-16 (005 US1, T013 to T023; spec FR-001 to FR-016, FR-037).

Every test starts from `pack_run`: the pack reconciled to `AWAITING_REVIEW` (21/4/2/2, proof
PASS) with four open items: CELL 4419 qty (CORRECTED 60 → 66), CELL 4428 qty (ILLEGIBLE),
FLAG_ACK 4429 sequence gap and FLAG_ACK 4430 backdated entry.
"""

import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from app.core.config import get_settings
from app.core.errors import InvalidReviewAction
from app.domain.enums import (
    AuditAction,
    CellStatus,
    FlagType,
    Outcome,
    ReadVariant,
    RegisterField,
    ReviewAction,
    ReviewReason,
    ReviewStatus,
    RunEventStatus,
    RunStatus,
    Source,
    Stage,
)
from app.domain.models import (
    Cell,
    Group,
    PageLabelMismatchContext,
    ReviewItem,
    RowInOneReadContext,
)
from app.services import review, stages
from tests.factories import T0, flag_review, make_review, make_run, mapping_review
from tests.fakes.api import FakeNotifier
from tests.support.pack_run import CONTROLLER, PackRun

API = "/api/v1"
Client = Callable[[str], TestClient]

QTY_4419 = "CELL:REG-p2-r05:qty"
QTY_4428 = "CELL:REG-p2-r14:qty"
GAP_4429 = "FLAG:SEQUENCE_GAP:4429"
BACKDATED_4430 = "FLAG:BACKDATED_ENTRY:ERP-0028"
REVIEWER_EMAIL = "reviewer@kestrel.example"


# --- helpers ---------------------------------------------------------------------------


def _item(pack: PackRun, key: str) -> ReviewItem:
    (item,) = [i for i in pack.repo.list_review_items(pack.run_id) if i.item_key == key]
    return item


def _group(pack: PackRun, grn: int) -> Group:
    (group,) = [g for g in pack.repo.list_groups(pack.run_id) if g.grn_no == grn]
    return group


def _resolve(
    client: TestClient, pack: PackRun, key: str, body: dict[str, Any], fault: str | None = None
) -> Response:
    params = {"fault": fault} if fault else None
    item_id = _item(pack, key).id
    return client.post(f"{API}/review-items/{item_id}/resolve", json=body, params=params)


def _ok(resp: Response) -> dict[str, Any]:
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


def _error(resp: Response, status: int, code: str) -> dict[str, Any]:
    assert resp.status_code == status, resp.text
    error: dict[str, Any] = resp.json()["error"]
    assert error["code"] == code, error
    return error


ACCEPT = {"action": "ACCEPT_READ"}
ENTER_250 = {"action": "ENTER_VALUE", "value": "250"}
ACK = {"action": "ACKNOWLEDGE"}


@pytest.fixture
def ops_chat(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("TELEGRAM_OPS_CHAT_ID", "ops-chat")
    monkeypatch.setenv("TELEGRAM_CONTROLLER_CHAT_ID", "controller-chat")
    get_settings.cache_clear()
    return "ops-chat"


# --- T013 to T019: acceptance ----------------------------------------------------------


def test_F08_AC1_queue_at_most_10_with_four_known(pack_run: PackRun, api_client_as: Client) -> None:
    resp = api_client_as("reviewer").get(
        f"{API}/runs/{pack_run.run_id}/review-items", params={"status": "OPEN"}
    )
    items = _ok_list(resp)
    keys = [i["item_key"] for i in items]
    assert len(items) <= 10
    assert {QTY_4428, QTY_4419, GAP_4429, BACKDATED_4430} <= set(keys)
    kinds = [i["kind"] for i in items]
    n_cell = kinds.count("CELL")
    assert kinds[:n_cell] == ["CELL"] * n_cell  # CELL items first (R2)
    assert all(i["status"] == "OPEN" for i in items)


def _ok_list(resp: Response) -> list[dict[str, Any]]:
    assert resp.status_code == 200, resp.text
    body: list[dict[str, Any]] = resp.json()
    return body


def test_F08_AC2_accept_read_4419_mismatched_outlier_sheet(
    pack_run: PackRun, api_client_as: Client
) -> None:
    body = _ok(_resolve(api_client_as("reviewer"), pack_run, QTY_4419, ACCEPT))
    assert body["item"]["status"] == "RESOLVED" and body["item"]["resolved_value"] == "66"
    assert body["item"]["resolved_by"] == REVIEWER_EMAIL
    group = _group(pack_run, 4419)
    assert group.outcome is Outcome.MISMATCHED
    assert [d.field.value for d in group.diffs] == ["qty"]
    assert group.diffs[0].outlier is Source.SHEET


def test_F08_AC3_enter_250_on_4428_matched_equals_erp_sheet(
    pack_run: PackRun, api_client_as: Client
) -> None:
    item = _item(pack_run, QTY_4428)
    _ok(_resolve(api_client_as("reviewer"), pack_run, QTY_4428, ENTER_250))
    assert _group(pack_run, 4428).outcome is Outcome.MATCHED
    (entry,) = [
        a
        for a in pack_run.repo.list_audit(run_id=pack_run.run_id)
        if a.action is AuditAction.REVIEW_RESOLVED
    ]
    assert entry.entity == "review_item" and entry.entity_id == str(item.id)
    assert entry.actor == REVIEWER_EMAIL and entry.at == pack_run.run.scenario_now
    assert entry.before is not None and entry.before["status"] == "OPEN"
    assert entry.after is not None and entry.after["equals_sources"] == ["ERP", "SHEET"]
    assert entry.after["resolved_value"] == "250" and entry.after["group_outcome"] == "MATCHED"


def _unchanged(pack: PackRun, events_before: int, key: str) -> None:
    assert _item(pack, key).status is ReviewStatus.OPEN
    assert len(pack.repo.list_run_events(pack.run_id)) == events_before  # no reconcile ran


def test_F08_AC4_accept_read_on_4428_returns_422(pack_run: PackRun, api_client_as: Client) -> None:
    events = len(pack_run.repo.list_run_events(pack_run.run_id))
    _error(
        _resolve(api_client_as("reviewer"), pack_run, QTY_4428, ACCEPT),
        422,
        "INVALID_REVIEW_ACTION",
    )
    _unchanged(pack_run, events, QTY_4428)


def test_F08_AC5_enter_abc_for_qty_returns_422(pack_run: PackRun, api_client_as: Client) -> None:
    events = len(pack_run.repo.list_run_events(pack_run.run_id))
    body = {"action": "ENTER_VALUE", "value": "abc"}
    _error(
        _resolve(api_client_as("reviewer"), pack_run, QTY_4419, body), 422, "INVALID_REVIEW_ACTION"
    )
    _unchanged(pack_run, events, QTY_4419)


def test_F08_AC6_resolved_items_survive_rereconcile(
    pack_run: PackRun, api_client_as: Client
) -> None:
    reviewer = api_client_as("reviewer")
    _ok(_resolve(reviewer, pack_run, QTY_4419, ACCEPT))
    _ok(_resolve(reviewer, pack_run, QTY_4428, ENTER_250))
    before = [_item(pack_run, k) for k in (QTY_4419, QTY_4428)]
    for _ in range(2):
        resp = api_client_as("controller").post(f"{API}/runs/{pack_run.run_id}/reconcile")
        assert resp.status_code == 200, resp.text
    after = [_item(pack_run, k) for k in (QTY_4419, QTY_4428)]
    assert after == before
    assert [i.resolved_value for i in after] == ["66", "250"]


def test_F08_AC7_all_resolved_sets_ready(pack_run: PackRun, api_client_as: Client) -> None:
    reviewer = api_client_as("reviewer")
    _ok(_resolve(reviewer, pack_run, QTY_4419, ACCEPT))
    _ok(_resolve(reviewer, pack_run, QTY_4428, ENTER_250))
    _ok(_resolve(reviewer, pack_run, GAP_4429, ACK))
    run = _ok(_resolve(reviewer, pack_run, BACKDATED_4430, {**ACK, "note": "keyed late"}))["run"]
    assert run["status"] == "READY" and run["open_review_items"] == 0
    assert {o: run["counts"][o] for o in ("MATCHED", "MISMATCHED", "MISSING", "NEEDS_REVIEW")} == {
        "MATCHED": 22,
        "MISMATCHED": 5,
        "MISSING": 2,
        "NEEDS_REVIEW": 0,
    }
    assert pack_run.repo.get_run(pack_run.run_id).status is RunStatus.READY


# --- T020: conflicts -------------------------------------------------------------------


def test_F08_FM09_second_reviewer_gets_first_decision(
    pack_run: PackRun, api_client_as: Client
) -> None:
    _ok(_resolve(api_client_as("controller"), pack_run, QTY_4419, ACCEPT))
    events = len(pack_run.repo.list_run_events(pack_run.run_id))
    second = {"action": "ENTER_VALUE", "value": "60"}
    error = _error(
        _resolve(api_client_as("reviewer"), pack_run, QTY_4419, second),
        409,
        "ITEM_ALREADY_RESOLVED",
    )
    first = error["details"]["item"]
    assert first["resolved_value"] == "66" and first["resolved_by"] == CONTROLLER.email
    assert len(pack_run.repo.list_run_events(pack_run.run_id)) == events  # nothing re-reconciled


def test_F08_R8_finalised_run_rejects_409(pack_run: PackRun, api_client_as: Client) -> None:
    pack_run.repo.transition_run(
        pack_run.run_id,
        RunStatus.FINALISED,
        expected={RunStatus.AWAITING_REVIEW},
        finalised_by=CONTROLLER.email,
        finalised_at=pack_run.run.scenario_now,
    )
    _error(
        _resolve(api_client_as("reviewer"), pack_run, QTY_4419, ACCEPT), 409, "RUN_STATE_CONFLICT"
    )
    assert _item(pack_run, QTY_4419).status is ReviewStatus.OPEN


def test_F08_R4_status_guard_409_when_not_awaiting_or_ready(
    pack_run: PackRun, api_client_as: Client
) -> None:
    pack_run.repo.transition_run(
        pack_run.run_id,
        RunStatus.FAILED,
        expected={RunStatus.AWAITING_REVIEW},
        failed_stage=Stage.PROOF,
        error_message="proof failed",
    )
    error = _error(
        _resolve(api_client_as("controller"), pack_run, GAP_4429, ACK), 409, "RUN_STATE_CONFLICT"
    )
    assert error["details"]["status"] == "FAILED"
    assert _item(pack_run, GAP_4429).status is ReviewStatus.OPEN


# --- T021: actions and values ----------------------------------------------------------

_CELL_ACTIONS = {ReviewAction.ACCEPT_READ, ReviewAction.ENTER_VALUE, ReviewAction.CANNOT_DETERMINE}
MATRIX: dict[ReviewReason, set[ReviewAction]] = {
    ReviewReason.ILLEGIBLE: _CELL_ACTIONS,
    ReviewReason.CORRECTED: _CELL_ACTIONS,
    ReviewReason.UNCLEAR: _CELL_ACTIONS,
    ReviewReason.READS_DISAGREE: _CELL_ACTIONS,
    ReviewReason.ROW_IN_ONE_READ: {ReviewAction.ACCEPT_READ, ReviewAction.CANNOT_DETERMINE},
    ReviewReason.PAGE_LABEL_MISMATCH: {ReviewAction.ACCEPT_READ, ReviewAction.CANNOT_DETERMINE},
    ReviewReason.UNKNOWN_SUPPLIER: {ReviewAction.ENTER_VALUE, ReviewAction.CANNOT_DETERMINE},
    ReviewReason.UNKNOWN_UNIT: {ReviewAction.ENTER_VALUE, ReviewAction.CANNOT_DETERMINE},
    ReviewReason.UNPARSEABLE_DATE: {ReviewAction.ENTER_VALUE, ReviewAction.CANNOT_DETERMINE},
    ReviewReason.UNMATCHED_NO_KEY: {ReviewAction.ENTER_VALUE, ReviewAction.CANNOT_DETERMINE},
    ReviewReason.CONFLICTING_ROWS_IN_SOURCE: {ReviewAction.CANNOT_DETERMINE},
    ReviewReason.SEQUENCE_GAP: {ReviewAction.ACKNOWLEDGE},
    ReviewReason.BACKDATED_ENTRY: {ReviewAction.ACKNOWLEDGE},
    ReviewReason.DATE_OUT_OF_PERIOD: {ReviewAction.ACKNOWLEDGE},
}
"""data-model.md 1.1 (Clarification 2, C24), written out independently of the code."""


def _item_for(reason: ReviewReason) -> ReviewItem:
    run_id = uuid4()
    if reason is ReviewReason.ROW_IN_ONE_READ:
        cells = {f: Cell(value="1", status=CellStatus.CLEAR) for f in RegisterField}
        ctx = RowInOneReadContext(page_no=1, row_index=3, read=ReadVariant.A, cells=cells)
        return make_review(
            run_id, "CELL:REG-p1-r03:row", field="row", reason=reason, read_a=None,
            read_b=None, context=ctx,
        )  # fmt: skip
    if reason is ReviewReason.PAGE_LABEL_MISMATCH:
        ctx2 = PageLabelMismatchContext(
            page_no=1,
            run_month="2026-09",
            month_written={ReadVariant.A: "Aug 2026", ReadVariant.B: "Aug 2026"},
            page_no_written={ReadVariant.A: "1", ReadVariant.B: "1"},
        )
        return make_review(
            run_id, "CELL:REG-p1:page", field="page", reason=reason, read_a=None,
            read_b=None, context=ctx2, source_row_id=None,
        )  # fmt: skip
    if reason in MATRIX and reason.value in FlagType.__members__:
        flag = flag_review(run_id, f"FLAG:{reason.value}:x")
        ctx3 = flag.context.model_copy(update={"flag": FlagType(reason.value)})
        return flag.model_copy(update={"reason": reason, "context": ctx3})
    if reason in (ReviewReason.UNMATCHED_NO_KEY, ReviewReason.CONFLICTING_ROWS_IN_SOURCE):
        from tests.factories import unmatched_review

        return unmatched_review(run_id, "ROW:XLS-r12").model_copy(update={"reason": reason})
    if reason.value.startswith("UNKNOWN_") or reason is ReviewReason.UNPARSEABLE_DATE:
        return mapping_review(run_id, "MAP:ERP-0001:supplier").model_copy(update={"reason": reason})
    return make_review(run_id, "CELL:REG-p1-r01:qty", reason=reason)


@pytest.mark.parametrize("reason", list(MATRIX))
@pytest.mark.parametrize("action", list(ReviewAction))
def test_F08_R4_action_matrix(reason: ReviewReason, action: ReviewAction) -> None:
    item = _item_for(reason)
    if action in MATRIX[reason]:
        review.check_action(item, action)
    else:
        with pytest.raises(InvalidReviewAction):
            review.check_action(item, action)


@pytest.mark.parametrize(
    ("field", "raw", "stored"),
    [
        ("qty", "250", "250"),
        ("qty", " 12.5 ", "12.5"),
        ("qty", "0", "0"),
        ("qty", "-1", None),
        ("qty", "abc", None),
        ("qty", "NaN", None),
        ("qty", "Infinity", None),
        ("date", "28/09", "28/09"),
        ("date", "31/09", None),  # not a real date
        ("date", "29/02", None),  # 2026 is not a leap year
        ("date", "1/9", None),
        ("date", "2026-09-28", None),
        ("lot_no", "l2609-128", "L2609-128"),
        ("lot_no", "L2609-12", None),
        ("unit", " Bags ", "Bags"),
        ("unit", "crates of joy", None),
        ("supplier", "Qasr Packaging", "Qasr Packaging"),
        ("supplier", "S-103", "S-103"),
        ("supplier", "S-999", None),
        ("supplier", "Acme Ltd", None),
        ("grn_no", "4421", "4421"),
        ("grn_no", "12", None),
        ("grn_no", "1234567", None),
        ("grn_no", "44a1", None),
        ("item", "anything", None),  # not an enterable field
    ],
)
def test_F08_R4_value_validators(field: str, raw: str, stored: str | None) -> None:
    run = make_run(month="2026-09")
    if stored is None:
        with pytest.raises(InvalidReviewAction):
            review.validate_value(field, raw, run)
    else:
        assert review.validate_value(field, raw, run) == stored


@pytest.mark.parametrize("note", [None, "", "   "])
def test_F08_R4_cannot_determine_requires_note(
    pack_run: PackRun, api_client_as: Client, note: str | None
) -> None:
    body = {"action": "CANNOT_DETERMINE", "note": note}
    _error(
        _resolve(api_client_as("reviewer"), pack_run, QTY_4428, body), 422, "INVALID_REVIEW_ACTION"
    )
    assert _item(pack_run, QTY_4428).status is ReviewStatus.OPEN


def test_F08_R4_enter_blank_value_returns_422(pack_run: PackRun, api_client_as: Client) -> None:
    body = {"action": "ENTER_VALUE", "value": "   "}
    _error(
        _resolve(api_client_as("reviewer"), pack_run, QTY_4428, body), 422, "INVALID_REVIEW_ACTION"
    )
    missing = {"action": "ENTER_VALUE"}
    _error(
        _resolve(api_client_as("reviewer"), pack_run, QTY_4428, missing),
        422,
        "INVALID_REVIEW_ACTION",
    )
    assert _item(pack_run, QTY_4428).status is ReviewStatus.OPEN


def test_F08_R4_cannot_determine_keeps_group_needs_review(
    pack_run: PackRun, api_client_as: Client
) -> None:
    body = {"action": "CANNOT_DETERMINE", "note": "Blot covers all digits"}
    item = _ok(_resolve(api_client_as("reviewer"), pack_run, QTY_4428, body))["item"]
    assert item["status"] == "CANNOT_DETERMINE" and item["resolved_value"] is None
    assert item["note"] == "Blot covers all digits"
    assert _group(pack_run, 4428).outcome is Outcome.NEEDS_REVIEW


def test_F08_R4_acknowledge_flag_only(pack_run: PackRun, api_client_as: Client) -> None:
    reviewer = api_client_as("reviewer")
    _error(_resolve(reviewer, pack_run, QTY_4419, ACK), 422, "INVALID_REVIEW_ACTION")
    item = _ok(_resolve(reviewer, pack_run, GAP_4429, ACK))["item"]
    assert item["status"] == "RESOLVED" and item["resolved_value"] is None
    assert item["note"] is None
    bad = {"action": "ACCEPT_READ", "value": "1"}  # a value only goes with ENTER_VALUE
    _error(_resolve(reviewer, pack_run, QTY_4419, bad), 422, "INVALID_REVIEW_ACTION")


# --- T022: cards, roles, page image ----------------------------------------------------


def _cards(client: TestClient, pack: PackRun, **params: str) -> list[dict[str, Any]]:
    return _ok_list(client.get(f"{API}/runs/{pack.run_id}/review-items", params=params))


def test_F08_R3_card_evidence_order_no_prefill(pack_run: PackRun, api_client_as: Client) -> None:
    (card,) = [c for c in _cards(api_client_as("reviewer"), pack_run) if c["item_key"] == QTY_4428]
    keys = list(card)
    assert keys.index("read_a") < keys.index("read_b") < keys.index("context")
    assert card["read_a"]["status"] == "ILLEGIBLE" and card["read_b"]["status"] == "ILLEGIBLE"
    ctx = card["context"]
    ctx_keys = list(ctx)
    assert ctx_keys[:2] == ["page_no", "row_index"] and (ctx["page_no"], ctx["row_index"]) == (
        2,
        14,
    )
    assert ctx_keys.index("other_sources") > ctx_keys.index("row_index")
    assert ctx["other_sources"] == {"ERP": "250 BDL", "SHEET": "250 BDL"}
    assert ctx["other_sources_label"] == "other sources, for reference"
    assert card["resolved_value"] is None and "value" not in ctx  # never pre-filled
    assert ctx["table_top"] is not None and ctx["table_bottom"] is not None


def test_F08_R3_card_equals_sources_after_decision(
    pack_run: PackRun, api_client_as: Client
) -> None:
    reviewer = api_client_as("reviewer")
    _ok(_resolve(reviewer, pack_run, QTY_4428, ENTER_250))
    (card,) = _cards(reviewer, pack_run, status="RESOLVED")
    assert card["item_key"] == QTY_4428 and card["context"]["equals_sources"] == ["ERP", "SHEET"]


def test_F08_R2_card_order(pack_run: PackRun, api_client_as: Client) -> None:
    reviewer = api_client_as("reviewer")
    _ok(_resolve(reviewer, pack_run, GAP_4429, ACK))
    assert [c["item_key"] for c in _cards(reviewer, pack_run)] == [
        QTY_4419,  # CELL by page, row
        QTY_4428,
        BACKDATED_4430,  # FLAG_ACK last
        GAP_4429,
    ]
    assert [c["item_key"] for c in _cards(reviewer, pack_run, status="RESOLVED")] == [GAP_4429]


def test_F08_roles_ops_403_reviewer_ok(pack_run: PackRun, api_client_as: Client) -> None:
    _error(_resolve(api_client_as("ops"), pack_run, GAP_4429, ACK), 403, "NOT_ALLOWED")
    _error(_resolve(api_client_as("stranger"), pack_run, GAP_4429, ACK), 403, "NOT_ALLOWED")
    assert _item(pack_run, GAP_4429).status is ReviewStatus.OPEN
    assert len(_cards(api_client_as("ops"), pack_run)) == 4  # reads are open to every role
    _ok(_resolve(api_client_as("reviewer"), pack_run, GAP_4429, ACK))


def test_F08_EP16_page_image_signed_link_and_edges(
    pack_run: PackRun, api_client_as: Client
) -> None:
    ops = api_client_as("ops")
    body = _ok(ops.get(f"{API}/runs/{pack_run.run_id}/pages/2/image"))
    (page2,) = [f for f in pack_run.repo.list_source_files(pack_run.run_id) if f.page_no == 2]
    settings = get_settings()
    read_a = pack_run.repo.get_extraction(
        page2.sha256, ReadVariant.A, settings.EXTRACT_MODEL, settings.PROMPT_VERSION
    )
    assert read_a is not None
    assert body["page_no"] == 2 and body["slots"] == 16
    assert page2.storage_path in body["signed_url"]
    assert body["expires_at"] == (T0 + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    assert (body["table_top"], body["table_bottom"]) == (
        read_a.result.table_top,
        read_a.result.table_bottom,
    )
    _error(ops.get(f"{API}/runs/{pack_run.run_id}/pages/7/image"), 404, "NOT_FOUND")
    _error(ops.get(f"{API}/runs/{uuid4()}/pages/1/image"), 404, "NOT_FOUND")


# --- T023: concurrency, proof failure, events, speed -----------------------------------


def test_F08_FR012_concurrent_decisions_both_reflected(
    pack_run: PackRun, api_client_as: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = stages.reconcile_stage
    calls: list[int] = []
    other = _item(pack_run, QTY_4428)

    def racing(*args: Any, **kwargs: Any) -> Any:
        result = real(*args, **kwargs)
        calls.append(1)
        if len(calls) == 1:  # another reviewer decides while this re-reconcile writes
            pack_run.repo.resolve_review_item(
                other.id,
                status=ReviewStatus.RESOLVED,
                resolved_value="250",
                resolved_by=REVIEWER_EMAIL,
                resolved_at=pack_run.run.scenario_now,
                note=None,
            )
        return result

    monkeypatch.setattr("app.services.review.reconcile_stage", racing)
    _ok(_resolve(api_client_as("controller"), pack_run, QTY_4419, ACCEPT))
    assert len(calls) == 2  # exactly one extra pass
    assert _group(pack_run, 4419).outcome is Outcome.MISMATCHED
    assert _group(pack_run, 4428).outcome is Outcome.MATCHED


def test_F08_R6_proof_fail_on_rereconcile_keeps_decision(
    faults_on: None,
    ops_chat: str,
    pack_run: PackRun,
    api_client_as: Client,
    fake_notifier: FakeNotifier,
) -> None:
    resp = _resolve(api_client_as("reviewer"), pack_run, QTY_4419, ACCEPT, fault="PROOF_DROP_ROW")
    body = _ok(resp)
    assert body["item"]["status"] == "RESOLVED" and body["item"]["resolved_value"] == "66"
    assert body["run"]["status"] == "FAILED" and body["run"]["failed_stage"] == "proof"
    assert _item(pack_run, QTY_4419).status is ReviewStatus.RESOLVED
    run = pack_run.repo.get_run(pack_run.run_id)
    assert run.status is RunStatus.FAILED and run.failed_stage is Stage.PROOF
    assert len(fake_notifier.texts(ops_chat)) == 1
    audits = [
        a
        for a in pack_run.repo.list_audit(run_id=pack_run.run_id)
        if a.action is AuditAction.REVIEW_RESOLVED
    ]
    assert len(audits) == 1  # the decision is on the record


def test_F08_R2_run_events_review_started_ok(pack_run: PackRun, api_client_as: Client) -> None:
    _ok(_resolve(api_client_as("reviewer"), pack_run, QTY_4419, ACCEPT))
    events = pack_run.repo.list_run_events(pack_run.run_id)  # newest first
    reviews = [e for e in events if e.stage is Stage.REVIEW]
    assert [e.status for e in reviews] == [RunEventStatus.OK, RunEventStatus.STARTED]
    assert reviews[0].detail["item_key"] == QTY_4419
    # the re-reconcile ran between review STARTED and OK
    started, ok = events.index(reviews[1]), events.index(reviews[0])
    assert any(e.stage is Stage.RECONCILE for e in events[ok:started])


def test_F08_NFR05_rereconcile_under_3s(pack_run: PackRun, api_client_as: Client) -> None:
    reviewer = api_client_as("reviewer")
    reviewer.get(f"{API}/runs/{pack_run.run_id}/review-items")  # warm the app
    start = time.perf_counter()
    _ok(_resolve(reviewer, pack_run, QTY_4428, ENTER_250))
    assert time.perf_counter() - start < 3.0


# --- unit: accept_read_value, equals_sources --------------------------------------------


def test_F08_R4_accept_read_value_rules() -> None:
    run_id = uuid4()
    corrected = Cell(value="66", status=CellStatus.CORRECTED, struck="60")
    agree = make_review(
        run_id, "CELL:REG-p1-r01:qty", reason=ReviewReason.CORRECTED, read_a=corrected,
        read_b=Cell(value=" 66 ", status=CellStatus.CLEAR),
    )  # fmt: skip
    assert review.accept_read_value(agree) == "66"
    disagree = make_review(run_id, "CELL:REG-p1-r01:qty", reason=ReviewReason.READS_DISAGREE)
    with pytest.raises(InvalidReviewAction):
        review.accept_read_value(disagree)
    assert review.accept_read_value(_item_for(ReviewReason.ROW_IN_ONE_READ)) is None
    assert review.accept_read_value(_item_for(ReviewReason.PAGE_LABEL_MISMATCH)) == "1"


def test_F08_R5_equals_sources_unit_level(pack_run: PackRun, api_client_as: Client) -> None:
    _ok(_resolve(api_client_as("reviewer"), pack_run, QTY_4419, ACCEPT))
    item = _item(pack_run, QTY_4419)
    assert review.equals_sources(pack_run.repo, pack_run.run_id, item) == [Source.ERP]
    flag = _item(pack_run, GAP_4429)
    assert review.equals_sources(pack_run.repo, pack_run.run_id, flag) == []
