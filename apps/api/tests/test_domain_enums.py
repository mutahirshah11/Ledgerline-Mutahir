"""Enums match data-model.md Section 2 exactly (FR-009)."""

import re
from enum import StrEnum

from app.domain import enums as e


def values(enum: type[StrEnum]) -> list[str]:
    return [m.value for m in enum]


def test_enum_source() -> None:
    assert values(e.Source) == ["REGISTER", "ERP", "SHEET"]


def test_enum_cell_status() -> None:
    assert values(e.CellStatus) == ["CLEAR", "UNCLEAR", "CORRECTED", "ILLEGIBLE"]


def test_enum_row_kind() -> None:
    assert values(e.RowKind) == ["RECEIPT", "DUPLICATE", "NON_RECEIPT"]


def test_enum_outcome() -> None:
    assert values(e.Outcome) == [
        "MATCHED",
        "MATCHED_AFTER_CONVERSION",
        "MISMATCHED",
        "MISSING",
        "NEEDS_REVIEW",
        "UNMATCHED_NO_KEY",
        "DUPLICATE",
        "NON_RECEIPT",
    ]


def test_enum_flag_type() -> None:
    assert values(e.FlagType) == [
        "SEQUENCE_GAP",
        "BACKDATED_ENTRY",
        "UNUSUAL_USER",
        "SUNDAY_DATE",
        "OUT_OF_ORDER",
        "DATE_OUT_OF_PERIOD",
        "UNKNOWN_SUPPLIER",
        "UNKNOWN_UNIT",
    ]


def test_enum_review_kind() -> None:
    assert values(e.ReviewKind) == ["CELL", "UNMATCHED_ROW", "UNKNOWN_MAPPING", "FLAG_ACK"]


def test_enum_review_status() -> None:
    assert values(e.ReviewStatus) == ["OPEN", "RESOLVED", "CANNOT_DETERMINE"]


def test_enum_run_status() -> None:
    assert values(e.RunStatus) == [
        "CREATED",
        "UPLOADED",
        "VALIDATED",
        "EXTRACTING",
        "EXTRACTED",
        "RECONCILED",
        "AWAITING_REVIEW",
        "READY",
        "FINALISED",
        "FAILED",
    ]


def test_enum_proof_status() -> None:
    assert values(e.ProofStatus) == ["PASS", "FAIL"]


def test_enum_notification_kind() -> None:
    assert values(e.NotificationKind) == ["SUMMARY", "ALERT"]


def test_enum_notification_status() -> None:
    assert values(e.NotificationStatus) == ["PENDING", "SENDING", "SENT", "FAILED"]


def test_enum_norm_code() -> None:
    assert values(e.NormCode) == [
        "GRN_PREFIX_STRIPPED",
        "DATE_SERIAL_PARSED",
        "DATE_TEXT_PARSED",
        "DATE_YEAR_FROM_HEADER",
        "SUPPLIER_MAPPED",
        "UNIT_MAPPED",
        "UNIT_CONVERTED",
        "TEXT_TRIMMED",
    ]


def test_enum_role() -> None:
    assert values(e.Role) == ["CONTROLLER", "REVIEWER", "OPS"]


def test_enum_audit_channel() -> None:
    assert values(e.AuditChannel) == ["WEB", "API", "SYSTEM", "TELEGRAM"]


def test_enum_run_event_status() -> None:
    assert values(e.RunEventStatus) == ["STARTED", "OK", "RETRY", "FAILED"]


def test_enum_read_variant() -> None:
    assert values(e.ReadVariant) == ["A", "B"]


def test_enum_review_action() -> None:
    assert values(e.ReviewAction) == [
        "ACCEPT_READ",
        "ENTER_VALUE",
        "CANNOT_DETERMINE",
        "ACKNOWLEDGE",
    ]


def test_enum_review_reason() -> None:
    assert values(e.ReviewReason) == [
        "ILLEGIBLE",
        "CORRECTED",
        "UNCLEAR",
        "READS_DISAGREE",
        "ROW_IN_ONE_READ",
        "PAGE_LABEL_MISMATCH",
        "UNKNOWN_SUPPLIER",
        "UNKNOWN_UNIT",
        "UNPARSEABLE_DATE",
        "UNMATCHED_NO_KEY",
        "CONFLICTING_ROWS_IN_SOURCE",
        "SEQUENCE_GAP",
        "BACKDATED_ENTRY",
        "DATE_OUT_OF_PERIOD",
    ]


def test_enum_audit_action() -> None:
    assert values(e.AuditAction) == [
        "RUN_CREATED",
        "FILE_UPLOADED",
        "RUN_VALIDATED",
        "PAGE_EXTRACTED",
        "RECONCILED",
        "REVIEW_RESOLVED",
        "RUN_FINALISED",
        "SUMMARY_SENT",
        "SUMMARY_FAILED",
        "ALERT_SENT",
    ]


def test_enum_problem_code() -> None:
    assert values(e.ProblemCode) == [
        "ENCODING",
        "MISSING_COLUMN",
        "NO_DATA_ROWS",
        "BAD_NUMBER",
        "BAD_DATE",
        "MISSING_WORKSHEET",
        "MISSING_HEADER",
        "IMAGE_UNREADABLE",
        "IMAGE_TOO_SMALL",
        "TOO_MANY_PAGES",
    ]


def test_enum_error_code() -> None:
    assert values(e.ErrorCode) == [
        "BAD_MONTH",
        "MISSING_SOURCE",
        "BAD_REQUEST",
        "CONFIRM_REQUIRED",
        "UNAUTHENTICATED",
        "NOT_ALLOWED",
        "NOT_FOUND",
        "RUN_STATE_CONFLICT",
        "DUPLICATE_RUN",
        "PROOF_NOT_PASSED",
        "ITEM_ALREADY_RESOLVED",
        "CONTRACT_VIOLATION",
        "INVALID_REVIEW_ACTION",
        "UPSTREAM_FAILED",
        "INTERNAL_ERROR",
    ]


def test_enum_upload_problem() -> None:
    assert values(e.UploadProblem) == ["FILE_MISSING", "UPLOAD_EXPIRED", "HASH_MISMATCH"]


def test_enum_stage_lowercase() -> None:
    assert values(e.Stage) == [
        "upload",
        "validate",
        "extract",
        "reconcile",
        "proof",
        "review",
        "finalise",
        "notify",
    ]


def test_enum_register_field() -> None:
    assert values(e.RegisterField) == [
        "date",
        "grn_no",
        "supplier",
        "item",
        "lot_no",
        "qty",
        "unit",
        "recd_by",
    ]


def test_enum_compared_field() -> None:
    assert values(e.ComparedField) == ["date", "supplier_code", "lot_no", "qty", "uom"]


def test_constants() -> None:
    f = e.RegisterField
    assert e.REVIEWABLE_REGISTER_FIELDS == frozenset(
        {f.DATE, f.GRN_NO, f.SUPPLIER, f.LOT_NO, f.QTY, f.UNIT}
    )
    assert e.PRINTED_ROW_SLOTS == 16


def test_all_values_upper_snake_except_stage_and_fields() -> None:
    lower = {e.Stage, e.RegisterField, e.ComparedField}
    all_enums = [
        obj
        for obj in vars(e).values()
        if isinstance(obj, type) and issubclass(obj, StrEnum) and obj is not StrEnum
    ]
    assert len(all_enums) == 26
    for enum in all_enums:
        pattern = r"^[a-z][a-z_]*$" if enum in lower else r"^[A-Z][A-Z0-9_]*$"
        for member in enum:
            assert re.fullmatch(pattern, member.value), (enum.__name__, member.value)
            assert member.name == member.value.upper()
