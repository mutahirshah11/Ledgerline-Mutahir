"""Small model factories for repo/storage tests. Fixed aware datetimes; never the real clock."""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from app.core.clock import KARACHI
from app.domain.enums import (
    AuditAction,
    AuditChannel,
    CellStatus,
    FlagType,
    NormCode,
    NotificationKind,
    NotificationStatus,
    Outcome,
    ProofStatus,
    ReadVariant,
    ReviewKind,
    ReviewReason,
    Role,
    RowKind,
    RunEventStatus,
    RunStatus,
    Source,
    Stage,
)
from app.domain.models import (
    CANONICAL_CELL_FIELDS,
    AllowedUser,
    AuditEntry,
    CanonicalRow,
    Cell,
    CellContext,
    CheckC1,
    CheckC2,
    CheckC3,
    CheckC4,
    CheckC5,
    ControlFlag,
    Extraction,
    FlagAckContext,
    Group,
    NormLogEntry,
    Notification,
    PageManifestEntry,
    PageRead,
    Proof,
    ProofChecks,
    ReconciliationPayload,
    ReviewItem,
    RowMeta,
    Run,
    RunEvent,
    SourceFile,
    UnknownMappingContext,
    UnmatchedRowContext,
)

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=KARACHI)


def at(minutes: int) -> datetime:
    """T0 plus `minutes`."""
    return T0 + timedelta(minutes=minutes)


def sha(n: int) -> str:
    return f"{n:064x}"


def make_run(**over: Any) -> Run:
    base: dict[str, Any] = dict(
        id=uuid4(),
        month="2026-09",
        status=RunStatus.CREATED,
        run_key=None,
        failed_stage=None,
        error_message=None,
        scenario_now=T0,
        created_by="controller@example.com",
        created_at=T0,
        finalised_at=None,
        finalised_by=None,
    )
    return Run(**(base | over))


def make_user(**over: Any) -> AllowedUser:
    base: dict[str, Any] = dict(
        email="reviewer@example.com", display_name="Reviewer", role=Role.REVIEWER
    )
    return AllowedUser(**(base | over))


def make_file(run_id: UUID, **over: Any) -> SourceFile:
    base: dict[str, Any] = dict(
        id=uuid4(),
        run_id=run_id,
        source=Source.ERP,
        filename="erp.csv",
        storage_path=f"inputs/{run_id}/erp.csv",
        sha256=sha(1),
        size_bytes=100,
        page_no=None,
        confirmed_at=None,
    )
    return SourceFile(**(base | over))


def make_page(run_id: UUID, **over: Any) -> PageManifestEntry:
    base: dict[str, Any] = dict(
        sha256=sha(7), month="2026-09", page_no=1, first_seen_run_id=run_id, first_seen_at=T0
    )
    return PageManifestEntry(**(base | over))


def make_extraction(**over: Any) -> Extraction:
    base: dict[str, Any] = dict(
        id=uuid4(),
        page_sha256=sha(7),
        read_variant=ReadVariant.A,
        model="model-x",
        prompt_version="v1",
        result=PageRead(
            month_written="SEP",
            page_no_written="1",
            table_top=0.1,
            table_bottom=0.9,
            blank_row_slots=16,
            rows=[],
        ),
        input_tokens=10,
        output_tokens=20,
        latency_ms=30,
        created_at=T0,
    )
    return Extraction(**(base | over))


def make_row(run_id: UUID, source_row_id: str, **over: Any) -> CanonicalRow:
    source = {"REG": Source.REGISTER, "ERP": Source.ERP, "XLS": Source.SHEET}[
        source_row_id.split("-")[0]
    ]
    base: dict[str, Any] = dict(
        id=uuid4(),
        run_id=run_id,
        source=source,
        source_row_id=source_row_id,
        row_kind=RowKind.RECEIPT,
        grn_no=None,
        date=None,
        supplier_code=None,
        item=None,
        lot_no=None,
        qty=Decimal("1.50"),
        uom="KG",
        qty_base=None,
        base_uom=None,
        cell_status=dict.fromkeys(CANONICAL_CELL_FIELDS, CellStatus.CLEAR),
        raw={"qty": "1.50"},
        meta=RowMeta(),
        duplicate_of=None,
        group_id=None,
        outcome=None,
    )
    return CanonicalRow(**(base | over))


def make_group(run_id: UUID, group_key: str, **over: Any) -> Group:
    base: dict[str, Any] = dict(
        id=uuid4(),
        run_id=run_id,
        group_key=group_key,
        grn_no=None,
        row_ids={},
        outcome=Outcome.MATCHED,
        missing_from=[],
        diffs=[],
    )
    return Group(**(base | over))


def make_flag(run_id: UUID, **over: Any) -> ControlFlag:
    base: dict[str, Any] = dict(
        id=uuid4(),
        run_id=run_id,
        flag=FlagType.SUNDAY_DATE,
        grn_no=None,
        source_row_id=None,
        detail={"k": "v"},
    )
    return ControlFlag(**(base | over))


def make_norm(run_id: UUID, source_row_id: str, **over: Any) -> NormLogEntry:
    base: dict[str, Any] = dict(
        id=uuid4(),
        run_id=run_id,
        source_row_id=source_row_id,
        field="grn_no",
        code=NormCode.GRN_PREFIX_STRIPPED,
        before="GRN-1",
        after="1",
    )
    return NormLogEntry(**(base | over))


def make_payload(run_id: UUID, n: int = 1) -> ReconciliationPayload:
    """Payload `n` (1 to 9): group GRN:n with two rows, one flag, one norm-log entry."""
    tag = str(n)
    group = make_group(run_id, f"GRN:{n}", grn_no=n, row_ids={Source.ERP: [f"ERP-000{n}"]})
    rows = [
        make_row(
            run_id, f"ERP-000{n}", group_id=group.id, outcome=Outcome.MATCHED, item=f"item-{tag}"
        ),
        make_row(run_id, f"XLS-r{n + 1}", group_id=None, item=f"item-{tag}"),
    ]
    return ReconciliationPayload(
        rows=rows,
        groups=[group],
        flags=[make_flag(run_id, detail={"tag": tag})],
        norm_log=[make_norm(run_id, rows[0].source_row_id, before=tag)],
    )


def cell(value: str = "12") -> Cell:
    return Cell(value=value, status=CellStatus.CLEAR)


def make_review(run_id: UUID, item_key: str, **over: Any) -> ReviewItem:
    base: dict[str, Any] = dict(
        id=uuid4(),
        run_id=run_id,
        kind=ReviewKind.CELL,
        item_key=item_key,
        grn_no=None,
        source_row_id="REG-p1-r01",
        field="qty",
        reason=ReviewReason.ILLEGIBLE,
        read_a=cell("12"),
        read_b=cell("13"),
        context=CellContext(page_no=1, row_index=1),
    )
    return ReviewItem(**(base | over))


def unmatched_review(run_id: UUID, item_key: str) -> ReviewItem:
    return make_review(
        run_id,
        item_key,
        kind=ReviewKind.UNMATCHED_ROW,
        reason=ReviewReason.UNMATCHED_NO_KEY,
        read_a=None,
        read_b=None,
        field=None,
        context=UnmatchedRowContext(group_key="LOT:x|S1", other_rows=[]),
    )


def mapping_review(run_id: UUID, item_key: str) -> ReviewItem:
    return make_review(
        run_id,
        item_key,
        kind=ReviewKind.UNKNOWN_MAPPING,
        reason=ReviewReason.UNKNOWN_SUPPLIER,
        read_a=None,
        read_b=None,
        context=UnknownMappingContext(source=Source.ERP, raw="ACME"),
    )


def flag_review(run_id: UUID, item_key: str) -> ReviewItem:
    return make_review(
        run_id,
        item_key,
        kind=ReviewKind.FLAG_ACK,
        reason=ReviewReason.SEQUENCE_GAP,
        read_a=None,
        read_b=None,
        field=None,
        context=FlagAckContext(flag=FlagType.SEQUENCE_GAP, detail={"gap": "5"}),
    )


def make_proof(run_id: UUID, **over: Any) -> Proof:
    checks = ProofChecks(
        C1=CheckC1(pass_=True, per_source={}),
        C2=CheckC2(pass_=True, rows=0, unplaced=[], placed_twice=[]),
        C3=CheckC3(pass_=True, per_source={}),
        C4=CheckC4(pass_=True, by_source_unit={}, qty_unknown=[]),
        C5=CheckC5(pass_=True, grns=0, gaps=[]),
    )
    base: dict[str, Any] = dict(
        run_id=run_id, status=ProofStatus.PASS, checks=checks, computed_at=T0
    )
    return Proof(**(base | over))


def make_notification(run_id: UUID, **over: Any) -> Notification:
    base: dict[str, Any] = dict(
        id=uuid4(),
        run_id=run_id,
        kind=NotificationKind.SUMMARY,
        idempotency_key=f"SUMMARY:{uuid4().hex}",
        status=NotificationStatus.PENDING,
        attempts=0,
        last_error=None,
        telegram_message_id=None,
        sent_at=None,
        updated_at=T0,
    )
    return Notification(**(base | over))


def make_event(run_id: UUID, **over: Any) -> RunEvent:
    base: dict[str, Any] = dict(
        id=uuid4(),
        run_id=run_id,
        stage=Stage.UPLOAD,
        status=RunEventStatus.OK,
        at=T0,
        duration_ms=5,
        detail={"n": 1},
    )
    return RunEvent(**(base | over))


def make_audit(**over: Any) -> AuditEntry:
    base: dict[str, Any] = dict(
        id=0,
        at=T0,
        actor="system",
        action=AuditAction.RUN_CREATED,
        entity="run",
        entity_id="x",
        run_id=None,
        before=None,
        after={"status": "CREATED"},
        channel=AuditChannel.SYSTEM,
    )
    return AuditEntry(**(base | over))
