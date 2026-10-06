"""Models enforce the data-model.md Section 4 invariants (FR-010, FR-011)."""

import datetime as dt
import json
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.domain import models as m
from app.domain.enums import (
    AuditAction,
    AuditChannel,
    CellStatus,
    FlagType,
    NotificationKind,
    NotificationStatus,
    Outcome,
    ProofStatus,
    ReadVariant,
    RegisterField,
    ReviewKind,
    ReviewReason,
    ReviewStatus,
    Role,
    RowKind,
    RunEventStatus,
    RunStatus,
    Source,
    Stage,
)

RUN_ID = UUID("7f3c0000-0000-4000-8000-000000000001")
OTHER_ID = UUID("7f3c0000-0000-4000-8000-000000000002")
PKT = dt.timezone(dt.timedelta(hours=5))
AT = dt.datetime(2026, 10, 2, 18, 47, tzinfo=PKT)
SHA = "a" * 64


def clear(value: str | None = "x") -> m.Cell:
    return m.Cell(value=value, status=CellStatus.CLEAR)


def row(index: int) -> m.RowRead:
    return m.RowRead(row_index=index, cells={f: clear() for f in RegisterField})


def page(**overrides: Any) -> m.PageRead:
    fields: dict[str, Any] = {
        "month_written": "Sep 2026", "page_no_written": "2", "table_top": 0.2,
        "table_bottom": 0.9, "blank_row_slots": 14, "rows": [row(1), row(2)],
    }
    fields.update(overrides)
    return m.PageRead(**fields)


def canonical(**overrides: Any) -> m.CanonicalRow:
    fields: dict[str, Any] = {
        "id": OTHER_ID, "run_id": RUN_ID, "source": Source.ERP, "source_row_id": "ERP-0027",
        "row_kind": RowKind.RECEIPT, "grn_no": 4425, "date": dt.date(2026, 9, 3),
        "supplier_code": "S-102", "item": "Denim", "lot_no": "L2609-002", "qty": "1200",
        "uom": "YD", "qty_base": "1097.28", "base_uom": "M",
        "cell_status": {"qty": CellStatus.CLEAR}, "raw": {"Qty": "1200"},
        "meta": m.RowMeta(entered_by="store.clerk", entered_at=AT),
        "duplicate_of": None, "group_id": None, "outcome": None,
    }
    fields.update(overrides)
    return m.CanonicalRow(**fields)


def group(**overrides: Any) -> m.Group:
    fields: dict[str, Any] = {
        "id": OTHER_ID, "run_id": RUN_ID, "group_key": "GRN:4425", "grn_no": 4425,
        "row_ids": {Source.ERP: ["ERP-0027"]}, "outcome": Outcome.MISSING,
        "missing_from": [Source.REGISTER, Source.SHEET], "diffs": [],
    }
    fields.update(overrides)
    return m.Group(**fields)


def review_item(**overrides: Any) -> m.ReviewItem:
    fields: dict[str, Any] = {
        "id": OTHER_ID, "run_id": RUN_ID, "kind": ReviewKind.CELL,
        "item_key": "CELL:REG-p2-r14:qty", "grn_no": 4425, "source_row_id": "REG-p2-r14",
        "field": "qty", "reason": ReviewReason.UNCLEAR, "read_a": None, "read_b": None,
        "context": m.CellContext(page_no=2, row_index=14, candidates=["120", "126"]),
    }
    fields.update(overrides)
    return m.ReviewItem(**fields)


# --- Decimal quantities (constitution, FR-011) -------------------------------------------


def test_qty_rejects_float() -> None:
    with pytest.raises(ValidationError):
        canonical(qty=1200.5)
    with pytest.raises(ValidationError):
        canonical(qty_base=1097.28)


def test_qty_accepts_string_and_int() -> None:
    assert canonical(qty="1200.50").qty == Decimal("1200.50")
    assert canonical(qty=1200).qty == Decimal("1200")
    assert canonical(qty=Decimal("7")).qty == Decimal("7")
    assert canonical(qty=None).qty is None


def test_decimal_serialises_as_string() -> None:
    dumped = canonical().model_dump(mode="json")
    assert dumped["qty_base"] == "1097.28"
    assert dumped["qty"] == "1200"
    assert '"qty_base":"1097.28"' in canonical().model_dump_json()
    assert canonical().model_dump()["qty_base"] == Decimal("1097.28")


# --- Cell -------------------------------------------------------------------------------


def test_cell_invariants() -> None:
    m.Cell(value="12", status=CellStatus.CLEAR)
    m.Cell(value=None, status=CellStatus.ILLEGIBLE, note="ink blot")
    m.Cell(value="126", status=CellStatus.CORRECTED, struck="120", note="overwritten")
    m.Cell(value=None, status=CellStatus.UNCLEAR, candidates=["120", "126"])
    bad: list[dict[str, Any]] = [
        {"value": "12", "status": CellStatus.CLEAR, "candidates": ["12"]},
        {"value": "12", "status": CellStatus.CLEAR, "struck": "11"},
        {"value": "12", "status": CellStatus.ILLEGIBLE},
        {"value": None, "status": CellStatus.ILLEGIBLE, "candidates": ["1"]},
        {"value": None, "status": CellStatus.CORRECTED, "struck": "120"},
        {"value": "126", "status": CellStatus.CORRECTED},
        {"value": "126", "status": CellStatus.CORRECTED, "struck": "120", "candidates": ["1"]},
        {"value": "120", "status": CellStatus.UNCLEAR, "candidates": ["120"]},
        {"value": None, "status": CellStatus.UNCLEAR},
        {"value": None, "status": CellStatus.UNCLEAR, "candidates": ["1"], "struck": "2"},
    ]
    for fields in bad:
        with pytest.raises(ValidationError):
            m.Cell(**fields)


# --- Register reads ---------------------------------------------------------------------


def test_row_read_requires_all_eight_fields() -> None:
    cells = {f: clear() for f in RegisterField if f is not RegisterField.RECD_BY}
    with pytest.raises(ValidationError):
        m.RowRead(row_index=1, cells=cells)
    with pytest.raises(ValidationError):
        m.RowRead(row_index=1, cells={**cells, RegisterField.RECD_BY: clear(), "extra": clear()})


def test_row_read_index_in_printed_slots() -> None:
    for index in (0, 17):
        with pytest.raises(ValidationError):
            row(index)


def test_page_read_unique_row_index() -> None:
    with pytest.raises(ValidationError):
        page(rows=[row(1), row(1)])


def test_page_read_rows_sorted_by_row_index() -> None:
    assert [r.row_index for r in page(rows=[row(3), row(1)]).rows] == [1, 3]


def test_page_read_table_bounds_ordered() -> None:
    page(table_top=None, table_bottom=None)
    page(table_top=0.1, table_bottom=None)
    for top, bottom in [(0.9, 0.2), (0.5, 0.5), (-0.1, 0.5), (0.2, 1.1)]:
        with pytest.raises(ValidationError):
            page(table_top=top, table_bottom=bottom)


def test_page_read_blank_row_slots_range() -> None:
    page(blank_row_slots=0)
    page(blank_row_slots=16)
    for slots in (-1, 17):
        with pytest.raises(ValidationError):
            page(blank_row_slots=slots)


def test_ground_truth_page_is_a_page_read() -> None:
    gt = m.GroundTruthPage(**page().model_dump(), page_no=2, source="ground_truth")
    assert isinstance(gt, m.PageRead)
    with pytest.raises(ValidationError):
        m.GroundTruthPage(**page().model_dump(), page_no=2, source="model")


def test_merged_row_holds_merged_cells() -> None:
    cell = clear("12")
    merged = m.MergedRow(
        page_no=2, row_index=14,
        cells={RegisterField.QTY: m.MergedCell(merged=cell, read_a=cell, read_b=None,
                                               review_reason=ReviewReason.ROW_IN_ONE_READ)},
    )
    assert merged.cells[RegisterField.QTY].read_b is None


# --- Reconciliation ---------------------------------------------------------------------


def test_group_missing_from_only_when_missing() -> None:
    group()
    group(outcome=Outcome.MATCHED, missing_from=[])
    with pytest.raises(ValidationError):
        group(outcome=Outcome.MATCHED, missing_from=[Source.SHEET])
    with pytest.raises(ValidationError):
        group(outcome=Outcome.MISSING, missing_from=[])


def test_group_row_ids_sorted() -> None:
    g = group(row_ids={Source.ERP: ["ERP-0030", "ERP-0027"]})
    assert g.row_ids[Source.ERP] == ["ERP-0027", "ERP-0030"]


def test_diff_shape() -> None:
    diff = m.Diff(
        field="qty",
        values={Source.REGISTER: "1200 YD", Source.ERP: "1097.28 M", Source.SHEET: "1200 M"},
        compared={Source.REGISTER: "1097.28 M", Source.ERP: "1097.28 M",
                  Source.SHEET: "1200.00 M"},
        outlier=Source.SHEET,
    )
    assert diff.model_dump(mode="json")["outlier"] == "SHEET"
    with pytest.raises(ValidationError):
        m.Diff(field="item", values={}, compared={}, outlier=None)


def test_reconciliation_payload() -> None:
    flag = m.ControlFlag(id=OTHER_ID, run_id=RUN_ID, flag=FlagType.BACKDATED_ENTRY, grn_no=4425,
                         source_row_id="ERP-0027", detail={"usual": "store.clerk"})
    log = m.NormLogEntry(id=OTHER_ID, run_id=RUN_ID, source_row_id="ERP-0027", field="uom",
                         code="UNIT_MAPPED", before="yds", after="YD")
    payload = m.ReconciliationPayload(rows=[canonical()], groups=[group()], flags=[flag],
                                      norm_log=[log])
    assert payload.norm_log[0].code == "UNIT_MAPPED"


# --- Review -----------------------------------------------------------------------------


def test_review_item_defaults_open() -> None:
    item = review_item()
    assert item.status is ReviewStatus.OPEN
    assert item.resolved_value is None and item.resolved_at is None


def test_review_item_cannot_determine_requires_note() -> None:
    with pytest.raises(ValidationError):
        review_item(status=ReviewStatus.CANNOT_DETERMINE, note=None)
    review_item(status=ReviewStatus.CANNOT_DETERMINE, note="page torn")


def test_review_context_matches_kind_and_reason() -> None:
    row_ctx = m.RowInOneReadContext(page_no=2, row_index=14, read=ReadVariant.A,
                                    cells={RegisterField.QTY: clear("12")})
    page_ctx = m.PageLabelMismatchContext(
        page_no=2, run_month="2026-09",
        month_written={ReadVariant.A: "Aug 2026", ReadVariant.B: "Aug 2026"},
        page_no_written={ReadVariant.A: "2", ReadVariant.B: "2"},
    )
    map_ctx = m.UnknownMappingContext(source=Source.ERP, raw="S-999")
    row_unmatched = m.UnmatchedRowContext(group_key="LOT:L2609-002|S-102", other_rows=[])
    flag_ctx = m.FlagAckContext(flag=FlagType.SEQUENCE_GAP, detail={"gap": "4427"})
    review_item(reason=ReviewReason.ROW_IN_ONE_READ, context=row_ctx)
    review_item(reason=ReviewReason.PAGE_LABEL_MISMATCH, context=page_ctx)
    review_item(kind=ReviewKind.UNKNOWN_MAPPING, reason=ReviewReason.UNKNOWN_SUPPLIER,
                context=map_ctx)
    review_item(kind=ReviewKind.UNMATCHED_ROW, reason=ReviewReason.UNMATCHED_NO_KEY,
                context=row_unmatched)
    review_item(kind=ReviewKind.FLAG_ACK, reason=ReviewReason.SEQUENCE_GAP, context=flag_ctx)
    with pytest.raises(ValidationError):
        review_item(reason=ReviewReason.UNCLEAR, context=row_ctx)
    with pytest.raises(ValidationError):
        review_item(kind=ReviewKind.FLAG_ACK, reason=ReviewReason.SEQUENCE_GAP, context=map_ctx)


def test_review_item_round_trips_through_json() -> None:
    item = review_item(kind=ReviewKind.UNKNOWN_MAPPING, reason=ReviewReason.UNKNOWN_UNIT,
                       context=m.UnknownMappingContext(source=Source.SHEET, raw="yrds"))
    again = m.ReviewItem.model_validate_json(item.model_dump_json())
    assert again == item
    assert isinstance(again.context, m.UnknownMappingContext)


# --- Proof ------------------------------------------------------------------------------


def proof() -> m.Proof:
    return m.Proof(
        run_id=RUN_ID, status=ProofStatus.PASS, computed_at=AT,
        checks=m.ProofChecks(
            C1=m.CheckC1(pass_=True, per_source={Source.ERP: m.SourceCount(in_=3, placed=3)}),
            C2=m.CheckC2(pass_=True, rows=3, unplaced=[], placed_twice=[]),
            C3=m.CheckC3(pass_=True, per_source={Source.ERP: 3}),
            C4=m.CheckC4(pass_=True, qty_unknown=[],
                         by_source_unit={"ERP|M": m.QtyBalance(in_="1097.28", placed="1097.28")}),
            C5=m.CheckC5(pass_=True, grns=3, gaps=[]),
        ),
    )


def test_proof_checks_use_blueprint_keys() -> None:
    dumped = proof().model_dump(mode="json")
    assert dumped["checks"]["C1"] == {"pass": True, "per_source": {"ERP": {"in": 3, "placed": 3}}}
    assert dumped["checks"]["C4"]["by_source_unit"]["ERP|M"] == {"in": "1097.28",
                                                                 "placed": "1097.28"}
    assert m.Proof.model_validate(json.loads(proof().model_dump_json())) == proof()


def test_proof_status_pass_only_when_all_checks_pass() -> None:
    good = proof()
    failing_c5 = good.checks.model_copy(update={"C5": m.CheckC5(pass_=False, grns=3, gaps=[4427])})
    m.Proof(run_id=RUN_ID, status=ProofStatus.FAIL, checks=failing_c5, computed_at=AT)
    with pytest.raises(ValidationError):
        m.Proof(run_id=RUN_ID, status=ProofStatus.PASS, checks=failing_c5, computed_at=AT)
    with pytest.raises(ValidationError):
        m.Proof(run_id=RUN_ID, status=ProofStatus.FAIL, checks=good.checks, computed_at=AT)


def test_proof_c4_rejects_float() -> None:
    with pytest.raises(ValidationError):
        m.QtyBalance(in_=1.5, placed="1.5")


# --- Runs, files, logs ------------------------------------------------------------------


def run(**overrides: Any) -> m.Run:
    fields: dict[str, Any] = {
        "id": RUN_ID, "month": "2026-09", "status": RunStatus.CREATED, "run_key": None,
        "failed_stage": None, "error_message": None, "scenario_now": AT,
        "created_by": "controller@example.com", "created_at": AT, "finalised_at": None,
        "finalised_by": None,
    }
    fields.update(overrides)
    return m.Run(**fields)


def test_run_month_format_and_stage() -> None:
    assert run(status=RunStatus.FAILED, failed_stage="extract").failed_stage is Stage.EXTRACT
    for month in ("2026-13", "2026-9", "Sep 2026"):
        with pytest.raises(ValidationError):
            run(month=month)
    with pytest.raises(ValidationError):
        run(failed_stage="EXTRACT")


def test_datetimes_must_be_timezone_aware() -> None:
    with pytest.raises(ValidationError):
        run(created_at=dt.datetime(2026, 10, 2, 18, 47))  # noqa: DTZ001


def test_source_file_page_no_only_for_register() -> None:
    base: dict[str, Any] = {
        "id": OTHER_ID, "run_id": RUN_ID, "filename": "p2.jpg", "storage_path": "x",
        "sha256": SHA, "size_bytes": 10, "confirmed_at": None,
    }
    m.SourceFile(**base, source=Source.REGISTER, page_no=2)
    m.SourceFile(**base, source=Source.ERP, page_no=None)
    with pytest.raises(ValidationError):
        m.SourceFile(**base, source=Source.ERP, page_no=2)
    with pytest.raises(ValidationError):
        m.SourceFile(**{**base, "sha256": "XYZ"}, source=Source.ERP, page_no=None)


def test_page_manifest_and_extraction() -> None:
    m.PageManifestEntry(sha256=SHA, month="2026-09", page_no=2, first_seen_run_id=RUN_ID,
                        first_seen_at=AT)
    ext = m.Extraction(id=OTHER_ID, page_sha256=SHA, read_variant=ReadVariant.B, model="m",
                       prompt_version="v1", result=page(), input_tokens=1, output_tokens=2,
                       latency_ms=3, created_at=AT)
    assert ext.result.rows[0].row_index == 1


def test_allowed_user_email_lower_case() -> None:
    assert m.AllowedUser(email="a@b.com", display_name="A", role=Role.OPS).role is Role.OPS
    with pytest.raises(ValidationError):
        m.AllowedUser(email="A@b.com", display_name="A", role=Role.OPS)


def test_notification_event_and_audit() -> None:
    n = m.Notification(id=OTHER_ID, run_id=RUN_ID, kind=NotificationKind.SUMMARY,
                       idempotency_key="SUMMARY:k", status=NotificationStatus.PENDING,
                       last_error=None, telegram_message_id=None, sent_at=None, updated_at=AT)
    assert n.attempts == 0
    m.RunEvent(id=OTHER_ID, run_id=RUN_ID, stage=Stage.PROOF, status=RunEventStatus.FAILED,
               at=AT, duration_ms=None, detail={"check": "C1"})
    m.AuditEntry(id=1, at=AT, actor="system", action=AuditAction.RUN_CREATED, entity="run",
                 entity_id=str(RUN_ID), run_id=RUN_ID, before=None, after={"status": "CREATED"},
                 channel=AuditChannel.SYSTEM)


def test_models_are_frozen() -> None:
    samples: list[Any] = [clear(), row(1), page(), canonical(), group(), review_item(), run()]
    for sample in samples:
        with pytest.raises(ValidationError):
            setattr(sample, next(iter(type(sample).model_fields)), None)
