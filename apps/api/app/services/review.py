"""F-08 human review (005 data-model.md §1, §2, §8; research R1 to R4, R16; blueprint C20, C24).

`resolve` is the EP-15 decision flow (plan "Key design points" 1): status guard, allowed action
for the item's reason, value (`ACCEPT_READ` / validated `ENTER_VALUE`), compare-and-set on the
`OPEN` item (FM-09), run events `review`, the Phase 3 `reconcile_stage` (one extra pass when
another decision landed meanwhile, FR-012), `equals_sources` and one `REVIEW_RESOLVED` audit.
`review_card_context` adds the EP-14 view fields to an item's stored context (never stored),
from one `CardData` load per request. Only current OPEN items (engine `open_review_items`) are
listed as open or decidable.
"""

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from app.core import clock
from app.core.config import get_settings
from app.core.db import Repo
from app.core.errors import InvalidReviewAction, ItemAlreadyResolved, StateConflict
from app.core.faults import Fault
from app.core.storage import BlobStore
from app.domain.enums import (
    AuditAction,
    AuditChannel,
    CellStatus,
    Outcome,
    ReadVariant,
    ReviewAction,
    ReviewKind,
    ReviewReason,
    ReviewStatus,
    RunStatus,
    Source,
    Stage,
)
from app.domain.models import (
    AllowedUser,
    CanonicalRow,
    Cell,
    Group,
    PageLabelMismatchContext,
    PageRead,
    ResolveRequest,
    ResolveResponse,
    ReviewDecisionAudit,
    ReviewItem,
    Run,
    RunView,
    SourceFile,
)
from app.pipeline.engine import open_review_state
from app.pipeline.normalise import _QTY_MAX_EXPONENT, load_mappings
from app.services.audit import audit
from app.services.finalise import load_run_view
from app.services.runlog import stage_step
from app.services.stages import reconcile_stage
from app.services.telegram import Notifier

Now = Callable[[], datetime]
log = logging.getLogger(__name__)

# --- allowed actions (data-model 1.1, Clarification 2, C24) ------------------------------

_CELL = frozenset(
    {ReviewAction.ACCEPT_READ, ReviewAction.ENTER_VALUE, ReviewAction.CANNOT_DETERMINE}
)
_READ_OR_UNDECIDED = frozenset({ReviewAction.ACCEPT_READ, ReviewAction.CANNOT_DETERMINE})
_ENTER_OR_UNDECIDED = frozenset({ReviewAction.ENTER_VALUE, ReviewAction.CANNOT_DETERMINE})
_ACK = frozenset({ReviewAction.ACKNOWLEDGE})

ALLOWED_ACTIONS: dict[ReviewReason, frozenset[ReviewAction]] = {
    ReviewReason.ILLEGIBLE: _CELL,
    ReviewReason.CORRECTED: _CELL,
    ReviewReason.UNCLEAR: _CELL,
    ReviewReason.READS_DISAGREE: _CELL,
    ReviewReason.ROW_IN_ONE_READ: _READ_OR_UNDECIDED,
    ReviewReason.PAGE_LABEL_MISMATCH: _READ_OR_UNDECIDED,
    ReviewReason.UNKNOWN_SUPPLIER: _ENTER_OR_UNDECIDED,
    ReviewReason.UNKNOWN_UNIT: _ENTER_OR_UNDECIDED,
    ReviewReason.UNPARSEABLE_DATE: _ENTER_OR_UNDECIDED,
    ReviewReason.UNMATCHED_NO_KEY: _ENTER_OR_UNDECIDED,
    ReviewReason.CONFLICTING_ROWS_IN_SOURCE: frozenset({ReviewAction.CANNOT_DETERMINE}),
    ReviewReason.SEQUENCE_GAP: _ACK,
    ReviewReason.BACKDATED_ENTRY: _ACK,
    ReviewReason.DATE_OUT_OF_PERIOD: _ACK,
}
"""Per reason (each reason belongs to one kind, so this is the kind matrix of data-model 1.1)."""


def field_of(item: ReviewItem) -> str:
    """The field an item decides (data-model 1.1): its stored `field`; `grn_no` for
    `UNMATCHED_NO_KEY` (stored with no field); else the kind's name."""
    if item.reason is ReviewReason.UNMATCHED_NO_KEY:
        return "grn_no"
    return item.field or item.kind.value


def check_action(item: ReviewItem, action: ReviewAction) -> None:
    """FR-004: `InvalidReviewAction` unless `action` is allowed for the item's kind/reason."""
    if action not in ALLOWED_ACTIONS[item.reason]:
        raise InvalidReviewAction(
            field_of(item), f"{action.value} is not allowed for {item.reason.value}"
        )


# --- values (data-model 1.2, research R16) ---------------------------------------------

_ACCEPTABLE = frozenset({CellStatus.CLEAR, CellStatus.CORRECTED})
_QTY = re.compile(r"[0-9]+(\.[0-9]+)?")  # ASCII digits only, as the engine reads them
_DATE = re.compile(r"[0-9]{2}/[0-9]{2}")
_LOT = re.compile(r"L[0-9]{4}-[0-9]{3}")
_GRN = re.compile(r"[0-9]{3,6}")


def _same(a: str | None, b: str | None) -> bool:
    return a is not None and b is not None and a.strip().casefold() == b.strip().casefold()


def accept_read_value(item: ReviewItem) -> str | None:
    """FR-005: the value both reads give (CLEAR or CORRECTED current value, equal after trim
    and case-fold), else `InvalidReviewAction`. `ROW_IN_ONE_READ` keeps the row as read (no
    value); `PAGE_LABEL_MISMATCH` takes the page label both reads wrote (C20, C24)."""
    field = field_of(item)
    if item.reason is ReviewReason.ROW_IN_ONE_READ:
        return None
    ctx = item.context
    if isinstance(ctx, PageLabelMismatchContext):
        a, b = ctx.page_no_written.get(ReadVariant.A), ctx.page_no_written.get(ReadVariant.B)
        if a is None or not _same(a, b):
            raise InvalidReviewAction(field, "the reads do not give one page label")
        return a.strip()
    read_a, read_b = item.read_a, item.read_b
    if not (_readable(read_a) and _readable(read_b)):
        raise InvalidReviewAction(field, "the reads do not give one value")
    assert read_a is not None and read_a.value is not None and read_b is not None
    if not _same(read_a.value, read_b.value):
        raise InvalidReviewAction(field, "the reads do not give one value")
    return read_a.value.strip()


def _readable(cell: Cell | None) -> bool:
    return cell is not None and cell.status in _ACCEPTABLE and cell.value is not None


def validate_value(field: str, raw: str, run: Run) -> str:
    """FR-006: the stored form of an entered value for `field`, else `InvalidReviewAction`.
    qty ASCII digits with an optional fraction, below the engine's limit (normalise
    `_QTY_MAX_EXPONENT`); date `DD/MM`, a real date in the run month's year; lot `L` 4 digits
    `-` 3 digits (upper-cased); unit an alias in `unit_map.csv`; supplier an alias in
    `supplier_map.csv` or a code listed there; `grn_no` 3 to 6 digits and not 0 (F-04.R3).
    Digits are ASCII only, as the engine reads them."""
    text = raw.strip()

    def bad(reason: str) -> InvalidReviewAction:
        return InvalidReviewAction(field, reason)

    if field == "qty":
        if not _QTY.fullmatch(text):
            raise bad("qty must be a number of 0 or more")
        if Decimal(text).adjusted() >= _QTY_MAX_EXPONENT:  # the engine would read it unclear
            raise bad("qty is too large")
        return text
    if field == "date":
        if not _DATE.fullmatch(text):
            raise bad("date must be DD/MM")
        day, month = (int(p) for p in text.split("/"))
        try:
            date(int(run.month[:4]), month, day)
        except ValueError:
            raise bad("date is not a real date") from None
        return text
    if field == "lot_no":
        lot = text.upper()
        if not _LOT.fullmatch(lot):
            raise bad("lot must look like L2609-128")
        return lot
    mappings = load_mappings()
    if field == "unit":
        if not text or mappings.unit_def(text) is None:
            raise bad("unit is not in the unit mapping file")
        return text
    if field == "supplier":
        if not text or (mappings.supplier_code(text) is None and text not in mappings.codes):
            raise bad("supplier is not in the supplier mapping file")
        return text
    if field == "grn_no":
        if not _GRN.fullmatch(text) or int(text) < 1:
            raise bad("GRN must be 3 to 6 digits")
        return text
    raise bad("a value cannot be entered for this field")


# --- other sources and equals_sources (research R4, C25) -------------------------------

_CANONICAL = {"qty": "qty", "unit": "uom", "supplier": "supplier_code", "lot_no": "lot_no"}


@dataclass
class CardData:
    """One request's reads for cards and `equals_sources`: the run's rows, groups and register
    files, each listed once, and read A per page fetched at most once (EP-14 N+1)."""

    repo: Repo
    run_id: UUID
    rows: dict[str, CanonicalRow]
    groups: Sequence[Group]
    pages: Sequence[SourceFile]
    _reads: dict[int, PageRead | None] = field(default_factory=dict)

    def row(self, item: ReviewItem) -> CanonicalRow | None:
        return None if item.source_row_id is None else self.rows.get(item.source_row_id)

    def group_of(self, item: ReviewItem, row: CanonicalRow | None) -> Group | None:
        if row is not None and row.group_id is not None:
            return next((g for g in self.groups if g.id == row.group_id), None)
        if item.grn_no is not None:
            return next((g for g in self.groups if g.grn_no == item.grn_no), None)
        return None

    def rows_by_source(self, group: Group) -> dict[Source, list[CanonicalRow]]:
        ids = [i for s in Source for i in group.row_ids.get(s, [])]
        rows = [self.rows[i] for i in ids if i in self.rows]
        return {s: [r for r in rows if r.source is s] for s in Source}

    def page_read_a(self, page_no: int) -> PageRead | None:
        if page_no not in self._reads:
            self._reads[page_no] = _read_a(self.repo, self.pages, page_no)
        return self._reads[page_no]


def load_card_data(repo: Repo, run_id: UUID) -> CardData:
    """The reads one EP-14 request (or one EP-15 decision after re-reconcile) needs."""
    rows = {r.source_row_id: r for r in repo.list_source_rows(run_id)}
    pages = repo.list_source_files(run_id, Source.REGISTER)
    return CardData(repo, run_id, rows, repo.list_groups(run_id), pages)


def _shown(row: CanonicalRow, field: str) -> str | None:
    """A row's canonical value of `field` as shown on the card."""
    if field == "qty":
        if row.qty is None:
            return None
        return f"{format(row.qty, 'f')} {row.uom}" if row.uom else format(row.qty, "f")
    if field == "date":
        return None if row.date is None else row.date.strftime("%d/%m")
    if field == "grn_no":
        return None if row.grn_no is None else str(row.grn_no)
    value = getattr(row, _CANONICAL.get(field, field), None)
    return None if value is None else str(value)


def _compared(row: CanonicalRow, field: str) -> object:
    """research R4: the value reconciliation compares for `field`."""
    if field == "qty":
        return None if row.qty_base is None else (row.qty_base, row.base_uom)
    if field == "date":
        return row.date
    if field == "grn_no":
        return row.grn_no
    return getattr(row, _CANONICAL.get(field, field), None)


def _other_sources(data: CardData, item: ReviewItem) -> dict[str, str | None]:
    """R3: the other sources' canonical values for the item's field in the same GRN group."""
    row = data.row(item)
    own = row.source if row is not None else Source.REGISTER
    group = data.group_of(item, row)
    by_source = data.rows_by_source(group) if group is not None else {}
    name = field_of(item)
    out: dict[str, str | None] = {}
    for source in Source:
        if source is own:
            continue
        shown = [v for r in by_source.get(source, []) if (v := _shown(r, name)) is not None]
        out[source.value] = ", ".join(shown) or None
    return out


def equals_sources(data: CardData, item: ReviewItem) -> list[Source]:
    """research R4: the other sources whose compared value of the item's field equals the
    decided row's, read after re-reconcile; [] without a value or a placed decided row."""
    if item.status is not ReviewStatus.RESOLVED or item.resolved_value is None:
        return []
    row = data.row(item)
    group = None if row is None else data.group_of(item, row)
    if row is None or group is None:
        return []
    by_source = data.rows_by_source(group)
    name = field_of(item)
    if name == "grn_no":  # membership: the decided row now groups with these sources
        return [s for s in Source if s is not row.source and by_source[s]]
    mine = _compared(row, name)
    if mine is None:
        return []
    return [
        s
        for s in Source
        if s is not row.source and any(_compared(r, name) == mine for r in by_source[s])
    ]


# --- EP-14 card (001 data-model "ReviewContext" view fields; R3) -----------------------

OTHER_SOURCES_LABEL = "other sources, for reference"
_VALUE_REASONS = frozenset(
    {
        ReviewReason.ILLEGIBLE,
        ReviewReason.CORRECTED,
        ReviewReason.UNCLEAR,
        ReviewReason.READS_DISAGREE,
        ReviewReason.UNKNOWN_SUPPLIER,
        ReviewReason.UNKNOWN_UNIT,
        ReviewReason.UNPARSEABLE_DATE,
    }
)
"""Items whose card shows `other_sources` (001 data-model "ReviewContext" view fields)."""


def _read_a(repo: Repo, pages: Sequence[SourceFile], page_no: int) -> PageRead | None:
    settings = get_settings()
    for f in pages:
        if f.page_no == page_no:
            read = repo.get_extraction(
                f.sha256, ReadVariant.A, settings.EXTRACT_MODEL, settings.PROMPT_VERSION
            )
            return None if read is None else read.result
    return None


def page_read_a(repo: Repo, run_id: UUID, page_no: int) -> PageRead | None:
    """The run's read A of register page `page_no` under the current model and prompt."""
    return _read_a(repo, repo.list_source_files(run_id, Source.REGISTER), page_no)


def review_card_context(data: CardData, item: ReviewItem) -> dict[str, Any]:
    """The item's stored context plus EP-14 view fields, in evidence order (R3): page and row
    first, the stored evidence, the page's table edges, then the other sources' values for
    reference (never a pre-filled value) and, after a decision, `equals_sources`."""
    ctx: dict[str, Any] = item.context.model_dump(mode="json")
    reason = item.reason
    if item.kind is ReviewKind.CELL and reason is not ReviewReason.PAGE_LABEL_MISMATCH:
        page_no = ctx["page_no"]
        ctx = {"page_no": page_no, "row_index": ctx["row_index"], **ctx}
        read = data.page_read_a(page_no)
        ctx["table_top"] = None if read is None else read.table_top
        ctx["table_bottom"] = None if read is None else read.table_bottom
    if reason in _VALUE_REASONS:
        ctx["other_sources"] = _other_sources(data, item)
        ctx["other_sources_label"] = OTHER_SOURCES_LABEL
        ctx["equals_sources"] = [s.value for s in equals_sources(data, item)]
    return ctx


def list_cards(
    repo: Repo, run_id: UUID, status: ReviewStatus | None
) -> list[tuple[ReviewItem, dict[str, Any]]]:
    """EP-14: the run's items in card order (F-08.R2) with their card context; OPEN items only
    when current (engine `open_review_items`), so the queue matches `open_review_items`."""
    listed = repo.list_review_items(run_id)
    ids = {i.id for i in open_review_state(repo, run_id, listed)[0]}
    items = [
        i
        for i in listed
        if (status is None or i.status is status)
        and (i.status is not ReviewStatus.OPEN or i.id in ids)
    ]
    data = load_card_data(repo, run_id)
    return [(item, review_card_context(data, item)) for item in items]


# --- EP-15 decision flow (plan "Key design points" 1) ----------------------------------

RunViewBuilder = Callable[[Repo, UUID], RunView]
_DECIDABLE = frozenset({RunStatus.AWAITING_REVIEW, RunStatus.READY})


def _fingerprint(items: Sequence[ReviewItem]) -> list[tuple[str, str, str | None]]:
    """research R3: every decision of the run (OPEN items are not decisions)."""
    return sorted(
        (i.item_key, i.status.value, i.resolved_value)
        for i in items
        if i.status is not ReviewStatus.OPEN
    )


_STABLE = frozenset({"get_extraction", "list_source_files"})


class _RequestReads:
    """The repo as one EP-15 request reads it (005 review I-7c, NFR-05). A stored page read
    never changes (`put_extraction` never overwrites), nor do the files of a run past `CREATED`
    (F-01, the 0002 triggers; EP-15 only decides on a later run), so each is fetched once per
    request. After `freeze` (every write done), the run view reuses the rows, groups, register
    files and review items read after re-reconcile, and makes any other read once."""

    def __init__(self, repo: Repo) -> None:
        self._repo = repo
        self._reads: dict[tuple[Any, ...], Any] = {}
        self._view: dict[tuple[Any, ...], Any] | None = None
        self.repo = cast(Repo, self)

    def freeze(self, data: CardData, items: Sequence[ReviewItem]) -> Repo:
        run_id = data.run_id
        self._view = {
            ("list_source_rows", (run_id,), ()): list(data.rows.values()),
            ("list_groups", (run_id,), ()): list(data.groups),
            ("list_source_files", (run_id, Source.REGISTER), ()): list(data.pages),
            ("list_review_items", (run_id,), ()): list(items),
        }
        return self.repo

    def __getattr__(self, name: str) -> Any:
        real = getattr(self._repo, name)
        view = self._view
        if name not in _STABLE and (view is None or not name.startswith(("get_", "list_"))):
            return real

        def read(*args: Any, **kwargs: Any) -> Any:
            key = (name, args, tuple(sorted(kwargs.items())))
            if key in self._reads:
                return self._reads[key]
            if view is not None and key in view:
                return view[key]
            found = real(*args, **kwargs)
            if name in _STABLE:
                if found is not None:  # a page read not stored yet may be stored later
                    self._reads[key] = found
            elif view is not None:
                view[key] = found
            return found

        return read


def _decision(
    item: ReviewItem, request: ResolveRequest, run: Run
) -> tuple[ReviewStatus, str | None]:
    check_action(item, request.action)
    if request.action is ReviewAction.ACCEPT_READ:
        return ReviewStatus.RESOLVED, accept_read_value(item)
    if request.action is ReviewAction.ENTER_VALUE:
        assert request.value is not None  # ResolveRequest rule
        return ReviewStatus.RESOLVED, validate_value(field_of(item), request.value, run)
    if request.action is ReviewAction.CANNOT_DETERMINE:
        return ReviewStatus.CANNOT_DETERMINE, None
    return ReviewStatus.RESOLVED, None


def _before(item: ReviewItem) -> dict[str, Any]:
    def read(cell: Cell | None) -> dict[str, Any] | None:
        return None if cell is None else {"value": cell.value, "status": cell.status.value}

    return {
        "item_key": item.item_key,
        "status": item.status.value,
        "resolved_value": item.resolved_value,
        "read_a": read(item.read_a),
        "read_b": read(item.read_b),
    }


def resolve(
    repo: Repo,
    blobs: BlobStore,
    notifier: Notifier,
    item_id: UUID,
    request: ResolveRequest,
    actor: AllowedUser,
    channel: AuditChannel,
    *,
    fault: Fault | None = None,
    run_view: RunViewBuilder = load_run_view,
    now: Now = clock.now,
) -> ResolveResponse:
    """EP-15 (F-08.R4 to R8, FM-09, FR-009 to FR-014). `RUN_STATE_CONFLICT` unless the run is
    `AWAITING_REVIEW` or `READY`; `ItemAlreadyResolved` when the item is decided (before or by
    a racing reviewer); `InvalidReviewAction` for a disallowed action or value, or an OPEN item
    the newest reconcile no longer produces. The audit entry is written after re-reconcile, and
    also when it raises (decision kept, outcome null; an audit error then is logged and the
    original error propagates)."""
    item = repo.get_review_item(item_id)
    run = repo.get_run(item.run_id)
    if run.status not in _DECIDABLE:
        raise StateConflict(run)
    if item.status is not ReviewStatus.OPEN:
        raise ItemAlreadyResolved(item)
    listed = repo.list_review_items(run.id)
    if item.id not in {i.id for i in open_review_state(repo, run.id, listed)[0]}:
        raise InvalidReviewAction(field_of(item), "item is no longer current")
    status, value = _decision(item, request, run)
    try:
        decided = repo.resolve_review_item(
            item.id,
            status=status,
            resolved_value=value,
            resolved_by=actor.email,
            resolved_at=now(),
            note=request.note,
        )
    except StateConflict as exc:
        if isinstance(exc.current, Run):  # finalised since the guard (migration 0003): no audit
            raise
        raise ItemAlreadyResolved(exc.current) from exc
    group_outcome: Outcome | None = None
    group_key: str | None = None
    equal: list[Source] = []
    failed = False
    try:
        detail = {"item_key": item.item_key, "action": request.action.value}
        # decisions known before re-reconcile: ours plus those listed by the guard (one that
        # landed in between only costs the extra pass below)
        seen = _fingerprint([decided if i.id == item.id else i for i in listed])
        reads = _RequestReads(repo)
        with stage_step(repo, run.id, Stage.REVIEW, detail, now=now):
            result = reconcile_stage(reads.repo, blobs, notifier, run, actor, fault, now=now)
            reviewed = repo.list_review_items(run.id)
            if result.status in _DECIDABLE and _fingerprint(reviewed) != seen:
                current = repo.get_run(run.id)
                reconcile_stage(reads.repo, blobs, notifier, current, actor, fault, now=now)
                reviewed = repo.list_review_items(run.id)
        decided = next(i for i in reviewed if i.id == item.id)
        data = load_card_data(reads.repo, run.id)
        group = data.group_of(decided, data.row(decided))
        if group is not None:
            group_key, group_outcome = group.group_key, group.outcome
        equal = equals_sources(data, decided)
    except BaseException:
        failed = True
        raise
    finally:
        after = ReviewDecisionAudit(
            item_key=item.item_key,
            action=request.action,
            status=status,
            resolved_value=value,
            note=request.note,
            equals_sources=equal,
            group_key=group_key,
            group_outcome=group_outcome,
        )
        try:
            audit(
                repo,
                actor.email,
                AuditAction.REVIEW_RESOLVED,
                "review_item",
                str(item.id),
                run.id,
                before=_before(item),
                after=after.model_dump(mode="json"),
                channel=channel,
                now=now,
            )
        except Exception:
            if not failed:
                raise
            log.exception("review audit failed after a failed re-reconcile")  # keep the cause
    return ResolveResponse(item=decided, run=run_view(reads.freeze(data, reviewed), run.id))
