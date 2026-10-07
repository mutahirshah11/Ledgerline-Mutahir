"""Phase 4 request/response and view models enforce data-model.md §2 (F-08, F-09, F-10, F-11)."""

import datetime as dt
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.domain import models as m
from app.domain.enums import (
    CellStatus,
    FlagType,
    NotificationKind,
    NotificationStatus,
    Outcome,
    ProofStatus,
    ReviewAction,
    ReviewKind,
    ReviewReason,
    ReviewStatus,
    RowKind,
    RunStatus,
    Source,
)

RUN_ID = UUID("7f3c0000-0000-4000-8000-000000000001")
ITEM_ID = UUID("7f3c0000-0000-4000-8000-000000000002")
PKT = dt.timezone(dt.timedelta(hours=5))
AT = dt.datetime(2026, 10, 2, 18, 47, tzinfo=PKT)


# --- ResolveRequest ---------------------------------------------------------------------


def test_resolve_request_enter_value_requires_value() -> None:
    with pytest.raises(ValidationError):
        m.ResolveRequest(action=ReviewAction.ENTER_VALUE, value=None, note=None)
    req = m.ResolveRequest(action=ReviewAction.ENTER_VALUE, value="250", note=None)
    assert req.value == "250"


def test_resolve_request_value_forbidden_outside_enter_value() -> None:
    with pytest.raises(ValidationError):
        m.ResolveRequest(action=ReviewAction.ACCEPT_READ, value="66", note=None)
    ok = m.ResolveRequest(action=ReviewAction.ACCEPT_READ, value=None, note=None)
    assert ok.value is None


def test_resolve_request_cannot_determine_requires_non_blank_note() -> None:
    with pytest.raises(ValidationError):
        m.ResolveRequest(action=ReviewAction.CANNOT_DETERMINE, value=None, note=None)
    with pytest.raises(ValidationError):
        m.ResolveRequest(action=ReviewAction.CANNOT_DETERMINE, value=None, note="   ")
    ok = m.ResolveRequest(action=ReviewAction.CANNOT_DETERMINE, value=None, note="no legible read")
    assert ok.note == "no legible read"


def test_resolve_request_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        m.ResolveRequest.model_validate(
            {"action": "ACKNOWLEDGE", "value": None, "note": None, "extra": 1}
        )


def test_resolve_request_value_and_note_max_lengths() -> None:
    """EP-15 schema: value maxLength 64, note maxLength 500."""
    m.ResolveRequest(action=ReviewAction.ENTER_VALUE, value="9" * 64, note="n" * 500)
    with pytest.raises(ValidationError):
        m.ResolveRequest(action=ReviewAction.ENTER_VALUE, value="9" * 65, note=None)
    with pytest.raises(ValidationError):
        m.ResolveRequest(action=ReviewAction.ACKNOWLEDGE, value=None, note="n" * 501)


# --- ReviewDecisionAudit ------------------------------------------------------------------


def test_review_decision_audit_equals_sources_sorted() -> None:
    audit = m.ReviewDecisionAudit(
        item_key="CELL:ERP-0027:qty",
        action=ReviewAction.ENTER_VALUE,
        status=ReviewStatus.RESOLVED,
        resolved_value="250",
        note=None,
        equals_sources=[Source.SHEET, Source.ERP],
        group_key="4428",
        group_outcome=Outcome.MATCHED,
    )
    assert audit.equals_sources == [Source.ERP, Source.SHEET]


# --- GroupRowView / GroupView --------------------------------------------------------------


def _group_row(**overrides: object) -> m.GroupRowView:
    fields: dict[str, object] = {
        "source_row_id": "ERP-0027",
        "source": Source.ERP,
        "row_kind": RowKind.RECEIPT,
        "grn_no": 4425,
        "date": dt.date(2026, 9, 3),
        "supplier_code": "S-102",
        "item": "Denim",
        "lot_no": "L2609-002",
        "qty": "1200",
        "uom": "YD",
        "qty_base": "1097.28",
        "base_uom": "M",
        "cell_status": {"qty": CellStatus.CLEAR},
        "raw": {"Qty": "1200"},
        "meta": m.RowMeta(entered_by="store.clerk", entered_at=AT),
        "duplicate_of": None,
        "outcome": Outcome.MATCHED,
        "flags": [FlagType.SEQUENCE_GAP],
    }
    fields.update(overrides)
    return m.GroupRowView(**fields)


def test_group_row_view_qty_serialises_as_string() -> None:
    row = _group_row()
    dumped = row.model_dump(mode="json")
    assert dumped["qty"] == "1200"
    assert dumped["qty_base"] == "1097.28"
    assert row.qty == Decimal("1200")


def test_group_view_rows_by_source() -> None:
    group = m.Group(
        id=ITEM_ID,
        run_id=RUN_ID,
        group_key="4425",
        grn_no=4425,
        row_ids={Source.ERP: ["ERP-0027"], Source.SHEET: ["XLS-r11"]},
        outcome=Outcome.MATCHED,
        missing_from=[],
        diffs=[],
    )
    view = m.GroupView(group=group, rows={Source.ERP: [_group_row()], Source.SHEET: []})
    assert view.rows[Source.ERP][0].source_row_id == "ERP-0027"
    assert view.rows[Source.SHEET] == []


# --- PageImageLink / ReportLinks / Finalise -------------------------------------------------


def test_page_image_link_fields() -> None:
    link = m.PageImageLink(
        page_no=2,
        signed_url="https://example/signed",
        expires_at=AT,
        table_top=0.2,
        table_bottom=0.9,
        slots=16,
    )
    assert link.slots == 16


def test_report_links_fields() -> None:
    links = m.ReportLinks(
        html_url="https://example/report.html",
        csv_url="https://example/outcomes.csv",
        expires_at=AT,
    )
    assert links.html_url.endswith("report.html")


def test_finalise_request_confirm_bool() -> None:
    req = m.FinaliseRequest(confirm=True)
    assert req.confirm is True
    with pytest.raises(ValidationError):
        m.FinaliseRequest.model_validate({"confirm": "banana"})


def test_finalise_request_confirm_strict_and_defaults_false() -> None:
    """Missing/false reach the route (ConfirmRequired, R14); coercible non-bools are rejected."""
    assert m.FinaliseRequest.model_validate({}).confirm is False
    assert m.FinaliseRequest.model_validate({"confirm": False}).confirm is False
    for bad in ("yes", "true", 1, 0):
        with pytest.raises(ValidationError):
            m.FinaliseRequest.model_validate({"confirm": bad})


def test_page_image_link_slots_const_16() -> None:
    fields = {
        "page_no": 2,
        "signed_url": "https://example/signed",
        "expires_at": AT,
        "table_top": None,
        "table_bottom": None,
    }
    assert m.PageImageLink.model_validate(fields).slots == 16
    with pytest.raises(ValidationError):
        m.PageImageLink.model_validate({**fields, "slots": 15})


# --- RunView -------------------------------------------------------------------------------


def _run_view(**overrides: object) -> m.RunView:
    fields: dict[str, object] = {
        "id": RUN_ID,
        "month": "2026-09",
        "status": RunStatus.AWAITING_REVIEW,
        "run_key": "2026-09",
        "failed_stage": None,
        "error_message": None,
        "scenario_now": AT,
        "created_by": "controller@example.com",
        "created_at": AT,
        "finalised_at": None,
        "finalised_by": None,
        "stage_progress": m.StageProgressView(stages=[], pages=[]),
        "counts": {Outcome.MATCHED: 21},
        "row_counts": {Source.ERP: {Outcome.MATCHED: 21}},
        "proof_status": ProofStatus.PASS,
        "open_review_items": 8,
        "headline": "29 receipts: 21 match, 8 need attention",
        "exceptions": [],
        "summary": None,
        "read_cost": m.ReadCost(
            new_pages=0, estimate_usd=None, actual_usd=None, input_tokens=0, output_tokens=0
        ),
    }
    fields.update(overrides)
    return m.RunView(**fields)


def test_run_view_flat_fields() -> None:
    view = _run_view()
    assert view.headline == "29 receipts: 21 match, 8 need attention"
    assert view.open_review_items == 8
    assert view.summary is None


def test_exception_item_fields() -> None:
    item = m.ExceptionItem(
        category=1,
        kind="MISSING",
        grn_no=4421,
        group_key="4421",
        source_row_id=None,
        field=None,
        flags=[],
        text="GRN 4421 missing from SHEET",
    )
    assert item.kind == "MISSING"
    for bad_category in (0, 6):
        with pytest.raises(ValidationError):
            m.ExceptionItem(
                category=bad_category,
                kind="MISSING",
                grn_no=None,
                group_key=None,
                source_row_id=None,
                field=None,
                flags=[],
                text="x",
            )
    with pytest.raises(ValidationError):
        m.ExceptionItem(
            category=1,
            kind="NOT_A_KIND",
            grn_no=None,
            group_key=None,
            source_row_id=None,
            field=None,
            flags=[],
            text="x",
        )


def test_resolve_response_and_finalise_response_compose() -> None:
    item = m.ReviewItem(
        id=ITEM_ID,
        run_id=RUN_ID,
        kind=ReviewKind.CELL,
        item_key="x",
        grn_no=None,
        source_row_id=None,
        field="qty",
        reason=ReviewReason.UNCLEAR,
        read_a=None,
        read_b=None,
        context=m.CellContext(page_no=2, row_index=5, candidates=["66", "99"]),
        status=ReviewStatus.OPEN,
    )
    response = m.ResolveResponse(item=item, run=_run_view())
    assert response.run.headline.startswith("29 receipts")

    notification = m.Notification(
        id=ITEM_ID,
        run_id=RUN_ID,
        kind=NotificationKind.SUMMARY,
        idempotency_key="SUMMARY:2026-09",
        status=NotificationStatus.SENT,
        attempts=1,
        last_error=None,
        telegram_message_id=101,
        sent_at=AT,
        updated_at=AT,
    )
    finalise_response = m.FinaliseResponse(
        run=_run_view(status=RunStatus.FINALISED),
        report_url="https://example/report.html",
        notification=notification,
    )
    assert finalise_response.notification.status == NotificationStatus.SENT
