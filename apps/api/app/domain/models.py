"""Shared Pydantic models (data-model.md Section 4).

Immutable; quantities are Decimal in Python and strings in JSON.
"""

import datetime as dt
from decimal import Decimal
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.domain.enums import (
    PRINTED_ROW_SLOTS,
    AuditAction,
    AuditChannel,
    CellStatus,
    ComparedField,
    FlagType,
    NormCode,
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


def _reject_float(value: object) -> object:
    if isinstance(value, float | bool):
        raise ValueError("quantities must be given as str, int or Decimal, never float")
    return value


DecimalStr = Annotated[
    Decimal,
    BeforeValidator(_reject_float),
    PlainSerializer(lambda d: format(d, "f"), return_type=str, when_used="json"),
]
"""Quantity or money: Decimal in Python, plain-notation string in JSON ("1097.28", never "1E+3");
float input is rejected."""

Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Month = Annotated[str, StringConstraints(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]
RowIndex = Annotated[int, Field(ge=1, le=PRINTED_ROW_SLOTS)]
PageNo = Annotated[int, Field(ge=1)]
Fraction = Annotated[float, Field(ge=0, le=1)]


class DomainModel(BaseModel):
    """Base of every shared model: frozen, unknown keys rejected.

    The freeze is shallow: attributes cannot be reassigned, but list and dict fields are
    ordinary mutable containers. Never mutate them; build a new model (`model_copy(update=...)`)
    instead. Because those fields are unhashable, models are not hashable either; key by id.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class AliasedModel(DomainModel):
    """For blueprint keys that are Python keywords (`in`, `pass`)."""

    model_config = ConfigDict(validate_by_name=True, validate_by_alias=True,
                              serialize_by_alias=True)


# --- 4.1 Register reads and ground truth -------------------------------------------------


class Cell(DomainModel):
    value: str | None
    status: CellStatus
    struck: str | None = None
    candidates: list[str] = []
    note: str | None = None

    @model_validator(mode="after")
    def _status_rules(self) -> Self:
        s = self.status
        if self.struck is not None and s is not CellStatus.CORRECTED:
            raise ValueError("struck is only set when status = CORRECTED")
        if self.candidates and s is not CellStatus.UNCLEAR:
            raise ValueError("candidates are only set when status = UNCLEAR")
        if s is CellStatus.ILLEGIBLE and self.value is not None:
            raise ValueError("ILLEGIBLE cell has no value")
        if s is CellStatus.CORRECTED and (self.value is None or self.struck is None):
            raise ValueError("CORRECTED cell needs value and struck")
        if s is CellStatus.UNCLEAR and (self.value is not None or not self.candidates):
            raise ValueError("UNCLEAR cell has no value and at least one candidate")
        return self


class RowRead(DomainModel):
    row_index: RowIndex
    cells: dict[RegisterField, Cell]

    @field_validator("cells")
    @classmethod
    def _all_fields(cls, cells: dict[RegisterField, Cell]) -> dict[RegisterField, Cell]:
        if set(cells) != set(RegisterField):
            raise ValueError("cells must hold all 8 register fields")
        return cells


class PageRead(DomainModel):
    month_written: str | None
    page_no_written: str | None
    table_top: Fraction | None
    table_bottom: Fraction | None
    blank_row_slots: Annotated[int, Field(ge=0, le=PRINTED_ROW_SLOTS)]
    rows: list[RowRead]

    @field_validator("rows")
    @classmethod
    def _sorted_unique(cls, rows: list[RowRead]) -> list[RowRead]:
        indexes = [r.row_index for r in rows]
        if len(set(indexes)) != len(indexes):
            raise ValueError("row_index must be unique within a page")
        return sorted(rows, key=lambda r: r.row_index)

    @model_validator(mode="after")
    def _bounds_ordered(self) -> Self:
        top, bottom = self.table_top, self.table_bottom
        if top is not None and bottom is not None and not top < bottom:
            raise ValueError("table_top must be above table_bottom")
        return self


class GroundTruthPage(PageRead):
    page_no: PageNo
    source: Literal["ground_truth"]


class MergedCell(DomainModel):
    merged: Cell
    read_a: Cell | None
    read_b: Cell | None
    review_reason: ReviewReason | None


class MergedRow(DomainModel):
    page_no: PageNo
    row_index: RowIndex
    cells: dict[RegisterField, MergedCell]


# --- 4.2 Canonical rows and reconciliation -----------------------------------------------


class RowMeta(DomainModel):
    page_no: int | None = None
    row_index: int | None = None
    received_by: str | None = None
    entered_by: str | None = None
    entered_at: AwareDatetime | None = None
    remarks: str | None = None


class CanonicalRow(DomainModel):
    id: UUID
    run_id: UUID
    source: Source
    source_row_id: str
    row_kind: RowKind
    grn_no: int | None
    date: dt.date | None
    supplier_code: str | None
    item: str | None
    lot_no: str | None
    qty: DecimalStr | None
    uom: str | None
    qty_base: DecimalStr | None
    base_uom: str | None
    cell_status: dict[str, CellStatus]
    raw: dict[str, str | None]
    meta: RowMeta
    duplicate_of: str | None
    group_id: UUID | None
    outcome: Outcome | None


class Diff(DomainModel):
    field: ComparedField
    values: dict[Source, str | None]
    compared: dict[Source, str | None]
    outlier: Source | None


class Group(DomainModel):
    id: UUID
    run_id: UUID
    group_key: str
    grn_no: int | None
    row_ids: dict[Source, list[str]]
    outcome: Outcome
    missing_from: list[Source]
    diffs: list[Diff]

    @field_validator("row_ids")
    @classmethod
    def _sorted(cls, row_ids: dict[Source, list[str]]) -> dict[Source, list[str]]:
        return {source: sorted(ids) for source, ids in row_ids.items()}

    @field_validator("missing_from")
    @classmethod
    def _sorted_unique_sources(cls, sources: list[Source]) -> list[Source]:
        if len(set(sources)) != len(sources):
            raise ValueError("missing_from has a duplicate source")
        order = list(Source)
        return sorted(sources, key=order.index)  # Source declaration order (= Postgres enum)

    @model_validator(mode="after")
    def _missing_only_when_missing(self) -> Self:
        if bool(self.missing_from) != (self.outcome is Outcome.MISSING):
            raise ValueError("missing_from is non-empty exactly when outcome = MISSING")
        return self


class ControlFlag(DomainModel):
    id: UUID
    run_id: UUID
    flag: FlagType
    grn_no: int | None
    source_row_id: str | None
    detail: dict[str, str]


class NormLogEntry(DomainModel):
    id: UUID
    run_id: UUID
    source_row_id: str
    field: str
    code: NormCode
    before: str | None
    after: str | None


class ReconciliationPayload(DomainModel):
    """Argument of `Repo.replace_reconciliation`."""

    rows: list[CanonicalRow]
    groups: list[Group]
    flags: list[ControlFlag]
    norm_log: list[NormLogEntry]


# --- 4.3 Review ----------------------------------------------------------------------------
# ReviewContext: stored fields only (Phase 0); EP-14 view fields are added at read time.


class CellContext(DomainModel):
    """CELL with ILLEGIBLE, CORRECTED, UNCLEAR or READS_DISAGREE."""

    page_no: PageNo
    row_index: RowIndex
    candidates: list[str] = []
    struck: str | None = None
    note: str | None = None


class RowInOneReadContext(DomainModel):
    page_no: PageNo
    row_index: RowIndex
    read: ReadVariant
    cells: dict[RegisterField, Cell]


class PageLabelMismatchContext(DomainModel):
    page_no: int
    run_month: Month
    month_written: dict[ReadVariant, str | None]
    page_no_written: dict[ReadVariant, str | None]


class UnknownMappingContext(DomainModel):
    source: Source
    raw: str


class UnmatchedRowContext(DomainModel):
    group_key: str
    other_rows: list[str]


class FlagAckContext(DomainModel):
    flag: FlagType
    detail: dict[str, str]


ReviewContext = (
    CellContext
    | RowInOneReadContext
    | PageLabelMismatchContext
    | UnknownMappingContext
    | UnmatchedRowContext
    | FlagAckContext
)

_CELL_REASONS = frozenset(
    {ReviewReason.ILLEGIBLE, ReviewReason.CORRECTED, ReviewReason.UNCLEAR,
     ReviewReason.READS_DISAGREE}
)
# Reason codes per kind: blueprint Section 20 "Where review items come from".
_REASONS_BY_KIND: dict[ReviewKind, frozenset[ReviewReason]] = {
    ReviewKind.UNKNOWN_MAPPING: frozenset(
        {ReviewReason.UNKNOWN_SUPPLIER, ReviewReason.UNKNOWN_UNIT, ReviewReason.UNPARSEABLE_DATE}
    ),
    ReviewKind.UNMATCHED_ROW: frozenset(
        {ReviewReason.UNMATCHED_NO_KEY, ReviewReason.CONFLICTING_ROWS_IN_SOURCE}
    ),
    # "the flag type": the flags that queue a FLAG_ACK item (blueprint Section 18)
    ReviewKind.FLAG_ACK: frozenset(
        {ReviewReason.SEQUENCE_GAP, ReviewReason.BACKDATED_ENTRY, ReviewReason.DATE_OUT_OF_PERIOD}
    ),
}
_CONTEXT_BY_KIND: dict[ReviewKind, type[DomainModel]] = {
    ReviewKind.UNKNOWN_MAPPING: UnknownMappingContext,
    ReviewKind.UNMATCHED_ROW: UnmatchedRowContext,
    ReviewKind.FLAG_ACK: FlagAckContext,
}


def _context_type(kind: ReviewKind, reason: ReviewReason) -> type[DomainModel] | None:
    """The context shape for this kind/reason, or None when the reason does not fit the kind."""
    if kind is not ReviewKind.CELL:
        return _CONTEXT_BY_KIND[kind] if reason in _REASONS_BY_KIND[kind] else None
    if reason in _CELL_REASONS:
        return CellContext
    if reason is ReviewReason.ROW_IN_ONE_READ:
        return RowInOneReadContext
    if reason is ReviewReason.PAGE_LABEL_MISMATCH:
        return PageLabelMismatchContext
    return None


class ReviewItem(DomainModel):
    id: UUID
    run_id: UUID
    kind: ReviewKind
    item_key: str
    grn_no: int | None
    source_row_id: str | None
    field: str | None
    reason: ReviewReason
    read_a: Cell | None
    read_b: Cell | None
    context: ReviewContext
    status: ReviewStatus = ReviewStatus.OPEN
    resolved_value: str | None = None
    resolved_by: str | None = None
    resolved_at: AwareDatetime | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _rules(self) -> Self:
        expected = _context_type(self.kind, self.reason)
        if expected is None or type(self.context) is not expected:
            raise ValueError(f"reason or context does not fit {self.kind}/{self.reason}")
        ctx = self.context
        if isinstance(ctx, FlagAckContext) and ctx.flag.value != self.reason.value:
            raise ValueError(f"reason {self.reason} does not fit flag {ctx.flag}")
        if self.status is ReviewStatus.CANNOT_DETERMINE and self.note is None:
            raise ValueError("CANNOT_DETERMINE needs a note")
        return self


# --- 4.3 Proof -----------------------------------------------------------------------------


class AliasedCheck(AliasedModel):
    pass_: bool = Field(alias="pass")


class SourceCount(AliasedModel):
    in_: int = Field(alias="in")
    placed: int


class QtyBalance(AliasedModel):
    in_: DecimalStr = Field(alias="in")
    placed: DecimalStr


class CheckC1(AliasedCheck):
    per_source: dict[Source, SourceCount]


class CheckC2(AliasedCheck):
    rows: int
    unplaced: list[str]
    placed_twice: list[str]


class CheckC3(AliasedCheck):
    per_source: dict[Source, int]


class CheckC4(AliasedCheck):
    by_source_unit: dict[str, QtyBalance]  # key "SOURCE|UNIT"
    qty_unknown: list[str]


class CheckC5(AliasedCheck):
    grns: int
    gaps: list[int]


class ProofChecks(DomainModel):
    C1: CheckC1
    C2: CheckC2
    C3: CheckC3
    C4: CheckC4
    C5: CheckC5

    def all_pass(self) -> bool:
        return all(c.pass_ for c in (self.C1, self.C2, self.C3, self.C4, self.C5))


class Proof(DomainModel):
    run_id: UUID
    status: ProofStatus
    checks: ProofChecks
    computed_at: AwareDatetime

    @model_validator(mode="after")
    def _status_matches_checks(self) -> Self:
        if (self.status is ProofStatus.PASS) != self.checks.all_pass():
            raise ValueError("status is PASS exactly when all five checks pass")
        return self


# --- 4.3 Notifications and logs ----------------------------------------------------------


class Notification(DomainModel):
    id: UUID
    run_id: UUID
    kind: NotificationKind
    idempotency_key: str
    status: NotificationStatus
    attempts: Annotated[int, Field(ge=0)] = 0
    last_error: str | None
    telegram_message_id: int | None
    sent_at: AwareDatetime | None
    updated_at: AwareDatetime


class RunEvent(DomainModel):
    id: UUID
    run_id: UUID
    stage: Stage
    status: RunEventStatus
    at: AwareDatetime
    duration_ms: int | None
    detail: dict[str, Any]


class AuditEntry(DomainModel):
    id: int
    at: AwareDatetime
    actor: str  # email or "system"
    action: AuditAction
    entity: str
    entity_id: str
    run_id: UUID | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    channel: AuditChannel


# --- 4.4 Runs, files, pages, extractions, users ------------------------------------------


class Run(DomainModel):
    id: UUID
    month: Month
    status: RunStatus
    run_key: str | None
    failed_stage: Stage | None
    error_message: str | None
    scenario_now: AwareDatetime
    created_by: str
    created_at: AwareDatetime
    finalised_at: AwareDatetime | None
    finalised_by: str | None


class SourceFile(DomainModel):
    id: UUID
    run_id: UUID
    source: Source
    filename: str
    storage_path: str
    sha256: Sha256Hex
    size_bytes: int
    page_no: PageNo | None
    confirmed_at: AwareDatetime | None

    @model_validator(mode="after")
    def _page_no_register_only(self) -> Self:
        if self.page_no is not None and self.source is not Source.REGISTER:
            raise ValueError("page_no is only set for REGISTER files")
        return self


class PageManifestEntry(DomainModel):
    sha256: Sha256Hex
    month: Month
    page_no: PageNo
    first_seen_run_id: UUID
    first_seen_at: AwareDatetime


class Extraction(DomainModel):
    id: UUID
    page_sha256: Sha256Hex
    read_variant: ReadVariant
    model: str
    prompt_version: str
    result: PageRead
    input_tokens: int
    output_tokens: int
    latency_ms: int
    created_at: AwareDatetime


class AllowedUser(DomainModel):
    email: str
    display_name: str
    role: Role

    @field_validator("email")
    @classmethod
    def _lower_case(cls, email: str) -> str:
        if email != email.lower():
            raise ValueError("email must be lower-case")
        return email
