"""Shared enums and constants (data-model.md Section 2).

Nothing outside these lists may appear in data.
"""

from enum import StrEnum

# --- 2.1 Blueprint Section 9 enums (Postgres enums) ----------------------------------------


class Source(StrEnum):
    REGISTER = "REGISTER"
    ERP = "ERP"
    SHEET = "SHEET"


class CellStatus(StrEnum):
    CLEAR = "CLEAR"
    UNCLEAR = "UNCLEAR"
    CORRECTED = "CORRECTED"
    ILLEGIBLE = "ILLEGIBLE"


class RowKind(StrEnum):
    RECEIPT = "RECEIPT"
    DUPLICATE = "DUPLICATE"
    NON_RECEIPT = "NON_RECEIPT"


class Outcome(StrEnum):
    MATCHED = "MATCHED"
    MATCHED_AFTER_CONVERSION = "MATCHED_AFTER_CONVERSION"
    MISMATCHED = "MISMATCHED"
    MISSING = "MISSING"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    UNMATCHED_NO_KEY = "UNMATCHED_NO_KEY"
    DUPLICATE = "DUPLICATE"
    NON_RECEIPT = "NON_RECEIPT"


class FlagType(StrEnum):
    SEQUENCE_GAP = "SEQUENCE_GAP"
    BACKDATED_ENTRY = "BACKDATED_ENTRY"
    UNUSUAL_USER = "UNUSUAL_USER"
    SUNDAY_DATE = "SUNDAY_DATE"
    OUT_OF_ORDER = "OUT_OF_ORDER"
    DATE_OUT_OF_PERIOD = "DATE_OUT_OF_PERIOD"
    UNKNOWN_SUPPLIER = "UNKNOWN_SUPPLIER"
    UNKNOWN_UNIT = "UNKNOWN_UNIT"


class ReviewKind(StrEnum):
    CELL = "CELL"
    UNMATCHED_ROW = "UNMATCHED_ROW"
    UNKNOWN_MAPPING = "UNKNOWN_MAPPING"
    FLAG_ACK = "FLAG_ACK"


class ReviewStatus(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    CANNOT_DETERMINE = "CANNOT_DETERMINE"


class RunStatus(StrEnum):
    CREATED = "CREATED"
    UPLOADED = "UPLOADED"
    VALIDATED = "VALIDATED"
    EXTRACTING = "EXTRACTING"
    EXTRACTED = "EXTRACTED"
    RECONCILED = "RECONCILED"
    AWAITING_REVIEW = "AWAITING_REVIEW"
    READY = "READY"
    FINALISED = "FINALISED"
    FAILED = "FAILED"


class ProofStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"


class Fault(StrEnum):
    """Section 25 fault names; shared by engine, extraction, API and Telegram (E3)."""

    EXTRACT_TIMEOUT = "EXTRACT_TIMEOUT"
    EXTRACT_BAD_JSON = "EXTRACT_BAD_JSON"
    TELEGRAM_DOWN = "TELEGRAM_DOWN"
    PROOF_DROP_ROW = "PROOF_DROP_ROW"


class NotificationKind(StrEnum):
    SUMMARY = "SUMMARY"
    ALERT = "ALERT"


class NotificationStatus(StrEnum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class NormCode(StrEnum):
    GRN_PREFIX_STRIPPED = "GRN_PREFIX_STRIPPED"
    DATE_SERIAL_PARSED = "DATE_SERIAL_PARSED"
    DATE_TEXT_PARSED = "DATE_TEXT_PARSED"
    DATE_YEAR_FROM_HEADER = "DATE_YEAR_FROM_HEADER"
    SUPPLIER_MAPPED = "SUPPLIER_MAPPED"
    UNIT_MAPPED = "UNIT_MAPPED"
    UNIT_CONVERTED = "UNIT_CONVERTED"
    TEXT_TRIMMED = "TEXT_TRIMMED"


# --- 2.2 Table enums (Postgres enums) ------------------------------------------------------


class Role(StrEnum):
    CONTROLLER = "CONTROLLER"
    REVIEWER = "REVIEWER"
    OPS = "OPS"


class AuditChannel(StrEnum):
    WEB = "WEB"
    API = "API"
    SYSTEM = "SYSTEM"
    TELEGRAM = "TELEGRAM"


class RunEventStatus(StrEnum):
    STARTED = "STARTED"
    OK = "OK"
    RETRY = "RETRY"
    FAILED = "FAILED"


class ReadVariant(StrEnum):
    A = "A"
    B = "B"


# --- 2.3 Python-only code lists (stored as text) -------------------------------------------


class ReviewAction(StrEnum):
    ACCEPT_READ = "ACCEPT_READ"
    ENTER_VALUE = "ENTER_VALUE"
    CANNOT_DETERMINE = "CANNOT_DETERMINE"
    ACKNOWLEDGE = "ACKNOWLEDGE"


class ReviewReason(StrEnum):
    ILLEGIBLE = "ILLEGIBLE"
    CORRECTED = "CORRECTED"
    UNCLEAR = "UNCLEAR"
    READS_DISAGREE = "READS_DISAGREE"
    ROW_IN_ONE_READ = "ROW_IN_ONE_READ"
    PAGE_LABEL_MISMATCH = "PAGE_LABEL_MISMATCH"
    UNKNOWN_SUPPLIER = "UNKNOWN_SUPPLIER"
    UNKNOWN_UNIT = "UNKNOWN_UNIT"
    UNPARSEABLE_DATE = "UNPARSEABLE_DATE"
    UNMATCHED_NO_KEY = "UNMATCHED_NO_KEY"
    CONFLICTING_ROWS_IN_SOURCE = "CONFLICTING_ROWS_IN_SOURCE"
    SEQUENCE_GAP = "SEQUENCE_GAP"
    BACKDATED_ENTRY = "BACKDATED_ENTRY"
    DATE_OUT_OF_PERIOD = "DATE_OUT_OF_PERIOD"


class AuditAction(StrEnum):
    RUN_CREATED = "RUN_CREATED"
    FILE_UPLOADED = "FILE_UPLOADED"
    RUN_VALIDATED = "RUN_VALIDATED"
    PAGE_EXTRACTED = "PAGE_EXTRACTED"
    RECONCILED = "RECONCILED"
    REVIEW_RESOLVED = "REVIEW_RESOLVED"
    RUN_FINALISED = "RUN_FINALISED"
    SUMMARY_SENT = "SUMMARY_SENT"
    SUMMARY_FAILED = "SUMMARY_FAILED"
    ALERT_SENT = "ALERT_SENT"


class ProblemCode(StrEnum):
    ENCODING = "ENCODING"
    MISSING_COLUMN = "MISSING_COLUMN"
    NO_DATA_ROWS = "NO_DATA_ROWS"
    BAD_NUMBER = "BAD_NUMBER"
    BAD_DATE = "BAD_DATE"
    MISSING_WORKSHEET = "MISSING_WORKSHEET"
    MISSING_HEADER = "MISSING_HEADER"
    IMAGE_UNREADABLE = "IMAGE_UNREADABLE"
    IMAGE_TOO_SMALL = "IMAGE_TOO_SMALL"
    TOO_MANY_PAGES = "TOO_MANY_PAGES"


class ErrorCode(StrEnum):
    BAD_MONTH = "BAD_MONTH"
    MISSING_SOURCE = "MISSING_SOURCE"
    BAD_REQUEST = "BAD_REQUEST"
    CONFIRM_REQUIRED = "CONFIRM_REQUIRED"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    NOT_ALLOWED = "NOT_ALLOWED"
    NOT_FOUND = "NOT_FOUND"
    RUN_STATE_CONFLICT = "RUN_STATE_CONFLICT"
    DUPLICATE_RUN = "DUPLICATE_RUN"
    PROOF_NOT_PASSED = "PROOF_NOT_PASSED"
    ITEM_ALREADY_RESOLVED = "ITEM_ALREADY_RESOLVED"
    CONTRACT_VIOLATION = "CONTRACT_VIOLATION"
    INVALID_REVIEW_ACTION = "INVALID_REVIEW_ACTION"
    UPSTREAM_FAILED = "UPSTREAM_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class UploadProblem(StrEnum):
    """`details.problem` of FM-01/FM-02 (blueprint C17)."""

    FILE_MISSING = "FILE_MISSING"
    UPLOAD_EXPIRED = "UPLOAD_EXPIRED"
    HASH_MISMATCH = "HASH_MISMATCH"


class Stage(StrEnum):
    """Pipeline stages; lower-case words as the blueprint writes them."""

    UPLOAD = "upload"
    VALIDATE = "validate"
    EXTRACT = "extract"
    RECONCILE = "reconcile"
    PROOF = "proof"
    REVIEW = "review"
    FINALISE = "finalise"
    NOTIFY = "notify"


class RegisterField(StrEnum):
    """Columns of a register row (F-03 schema)."""

    DATE = "date"
    GRN_NO = "grn_no"
    SUPPLIER = "supplier"
    ITEM = "item"
    LOT_NO = "lot_no"
    QTY = "qty"
    UNIT = "unit"
    RECD_BY = "recd_by"


class ComparedField(StrEnum):
    """Canonical fields compared across sources (F-05.R6)."""

    DATE = "date"
    SUPPLIER_CODE = "supplier_code"
    LOT_NO = "lot_no"
    QTY = "qty"
    UOM = "uom"


# --- Constants -----------------------------------------------------------------------------

REVIEWABLE_REGISTER_FIELDS: frozenset[RegisterField] = frozenset(
    {
        RegisterField.DATE,
        RegisterField.GRN_NO,
        RegisterField.SUPPLIER,
        RegisterField.LOT_NO,
        RegisterField.QTY,
        RegisterField.UNIT,
    }
)
"""Register fields a reviewer can decide (blueprint Section 20)."""

PRINTED_ROW_SLOTS = 16
"""Data row slots printed on one register page."""

ROW_ONLY_OUTCOMES = frozenset({Outcome.DUPLICATE, Outcome.NON_RECEIPT})
"""Outcomes a row gets without a group (Section 8 count table, "n/a" groups)."""
