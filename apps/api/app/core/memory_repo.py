"""In-memory `Repo` (contracts/repo-interface.md) for tests and local runs.

Deep copies in and out (G1); dict indexes for unique keys (G5), raising `UniqueViolation`
with the constraint names of `supabase/migrations/0001_init.sql`. Batch writes
(`replace_reconciliation`, `upsert_review_items`) validate everything first and write only
after success, so a failure leaves nothing changed. The audit log is an append-only tuple.
Every public method holds one re-entrant lock: FastAPI runs sync routes on a threadpool.
"""

import functools
import threading
from collections.abc import Callable, Collection, Iterator
from datetime import datetime
from typing import TYPE_CHECKING, Any, NoReturn, TypeVar
from uuid import UUID

from pydantic import BaseModel

from app.core.errors import AppendOnlyViolation, NotFound, StateConflict, UniqueViolation
from app.domain.enums import (
    NotificationKind,
    NotificationStatus,
    Outcome,
    ReadVariant,
    ReviewKind,
    ReviewStatus,
    RunStatus,
    Source,
)
from app.domain.lifecycle import can_transition, unreconciled_decisions
from app.domain.models import (
    AllowedUser,
    AuditEntry,
    CanonicalRow,
    ControlFlag,
    Extraction,
    Group,
    NormLogEntry,
    Notification,
    PageManifestEntry,
    Proof,
    ReconciliationPayload,
    ReviewItem,
    Run,
    RunEvent,
    SourceFile,
)

M = TypeVar("M", bound=BaseModel)

_SOURCE_ORDER = {Source.REGISTER: 0, Source.ERP: 1, Source.SHEET: 2}
_KIND_ORDER = {
    ReviewKind.CELL: 0,
    ReviewKind.UNMATCHED_ROW: 1,
    ReviewKind.UNKNOWN_MAPPING: 2,
    ReviewKind.FLAG_ACK: 3,
}
_RUN_UPDATABLE_FIELDS = frozenset({"run_key", "error_message", "failed_stage"})
_RUN_TRANSITION_FIELDS = _RUN_UPDATABLE_FIELDS | {"finalised_at", "finalised_by"}
_NOTIFICATION_FIXED_FIELDS = frozenset({"id", "run_id", "kind", "idempotency_key"})
_REVIEW_REFRESHED_FIELDS = tuple("reason read_a read_b context grn_no source_row_id field".split())

ExtractionKey = tuple[str, ReadVariant, str, str]


def _copy(model: M) -> M:
    return model.model_copy(deep=True)


def _updated(model: M, fields: dict[str, Any]) -> M:
    """A validated copy of `model` with `fields` replaced (model_copy alone does not validate)."""
    data = dict(model)  # field values, not dumped, so nested models keep their types
    data.update(fields)
    return type(model).model_validate(data).model_copy(deep=True)


class _AppendOnlyLog:
    """Audit storage: an immutable tuple that only grows. Item assignment and deletion raise.

    Reads (`__iter__`, `__getitem__`) both hand out deep copies.
    """

    __slots__ = ("_entries",)

    def __init__(self) -> None:
        self._entries: tuple[AuditEntry, ...] = ()

    def append(self, entry: AuditEntry) -> None:
        self._entries = (*self._entries, entry)

    def __iter__(self) -> Iterator[AuditEntry]:
        return (e.model_copy(deep=True) for e in self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __getitem__(self, index: int) -> AuditEntry:
        return self._entries[index].model_copy(deep=True)

    def _refuse(self, *_: object) -> NoReturn:
        raise AppendOnlyViolation("audit_log")

    __setitem__ = __delitem__ = clear = pop = remove = insert = extend = _refuse


class _Reconciliation:
    """One run's rebuilt reconcile outputs (replaced as a unit)."""

    __slots__ = ("rows", "groups", "flags", "norm_log")

    def __init__(self) -> None:
        self.rows: dict[str, CanonicalRow] = {}  # source_row_id -> row
        self.groups: dict[str, Group] = {}  # group_key -> group
        self.flags: list[ControlFlag] = []
        self.norm_log: list[NormLogEntry] = []


def _recon_keys(
    rows: Collection[CanonicalRow],
    groups: Collection[Group],
    flags: Collection[ControlFlag],
    norm_log: Collection[NormLogEntry],
) -> list[tuple[str, list[object]]]:
    """Unique keys of reconcile outputs as (constraint, values), in the SQL report order."""
    return [
        ("source_rows_run_row_uq", [r.source_row_id for r in rows]),
        ("source_rows_pkey", [r.id for r in rows]),
        ("groups_run_group_key_uq", [g.group_key for g in groups]),
        ("groups_pkey", [g.id for g in groups]),
        ("control_flags_pkey", [f.id for f in flags]),
        ("normalisation_log_pkey", [n.id for n in norm_log]),
    ]


class MemoryRepo:
    """Dict-backed `Repo`. Stored models are never handed out; callers get deep copies."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._users: dict[str, AllowedUser] = {}
        self._runs: dict[UUID, Run] = {}
        self._files: dict[UUID, SourceFile] = {}
        self._file_keys: dict[tuple[UUID, str], UUID] = {}
        self._pages: dict[str, PageManifestEntry] = {}
        self._extractions: dict[ExtractionKey, Extraction] = {}
        self._extraction_ids: set[UUID] = set()
        self._recon: dict[UUID, _Reconciliation] = {}
        self._proofs: dict[UUID, Proof] = {}
        self._reviews: dict[UUID, ReviewItem] = {}
        self._review_keys: dict[tuple[UUID, str], UUID] = {}
        self._notifications: dict[UUID, Notification] = {}
        self._notification_keys: dict[str, UUID] = {}
        self._events: list[RunEvent] = []
        self._event_ids: set[UUID] = set()
        self._audit = _AppendOnlyLog()

    # --- helpers -------------------------------------------------------------------------

    def _run(self, run_id: UUID) -> Run:
        try:
            return self._runs[run_id]
        except KeyError:
            raise NotFound("run", run_id) from None

    def _check_finalised_key(self, run: Run) -> None:
        if run.status is not RunStatus.FINALISED or run.run_key is None:
            return
        for other in self._runs.values():
            if (
                other.id != run.id
                and other.status is RunStatus.FINALISED
                and other.run_key == run.run_key
            ):
                raise UniqueViolation("runs_run_key_finalised_uq")

    # --- health --------------------------------------------------------------------------

    def ping(self) -> bool:
        return True

    # --- users ---------------------------------------------------------------------------

    def get_allowed_user(self, email: str) -> AllowedUser | None:
        user = self._users.get(email)
        return None if user is None else _copy(user)

    def upsert_allowed_user(self, user: AllowedUser) -> AllowedUser:
        self._users[user.email] = _copy(user)
        return _copy(user)

    # --- runs ----------------------------------------------------------------------------

    def create_run(self, run: Run) -> Run:
        if run.id in self._runs:
            raise UniqueViolation("runs_pkey")
        self._check_finalised_key(run)
        self._runs[run.id] = _copy(run)
        return _copy(run)

    def get_run(self, run_id: UUID) -> Run:
        return _copy(self._run(run_id))

    def list_runs(self, limit: int = 20) -> list[Run]:
        by_id = sorted(self._runs.values(), key=lambda r: r.id)
        ordered = sorted(by_id, key=lambda r: r.created_at, reverse=True)  # stable: id asc
        return [_copy(r) for r in ordered[:limit]]

    def transition_run(
        self, run_id: UUID, to: RunStatus, *, expected: set[RunStatus], **fields: Any
    ) -> Run:
        current = self._run(run_id)
        bad = set(fields) - _RUN_TRANSITION_FIELDS
        if bad:
            raise ValueError(f"transition_run cannot set {sorted(bad)}")
        if current.status not in expected or not can_transition(current.status, to):
            raise StateConflict(_copy(current))
        if to is RunStatus.FINALISED and self._unreconciled(run_id):  # migration 0003
            raise StateConflict(_copy(current))
        updates = dict(fields)
        if current.status is RunStatus.FAILED:
            passed = {"failed_stage", "error_message"}.intersection(fields)
            if passed:
                raise ValueError(f"leaving FAILED clears {sorted(passed)}; do not pass them")
            updates.update(failed_stage=None, error_message=None)
        updates["status"] = to
        new = _updated(current, updates)
        self._check_finalised_key(new)
        self._runs[run_id] = new
        return _copy(new)

    def _unreconciled(self, run_id: UUID) -> bool:
        """A decided review item the newest `RECONCILED` audit has not applied (IMP-1)."""
        items = [i for i in self._reviews.values() if i.run_id == run_id]
        audit = [e for e in self._audit if e.run_id == run_id]
        return bool(unreconciled_decisions(items, audit))

    def update_run_fields(self, run_id: UUID, **fields: Any) -> Run:
        current = self._run(run_id)
        bad = set(fields) - _RUN_UPDATABLE_FIELDS
        if bad:
            raise ValueError(f"update_run_fields cannot set {sorted(bad)}")
        if current.status is RunStatus.FINALISED:
            raise StateConflict(_copy(current))
        new = _updated(current, fields)
        self._runs[run_id] = new
        return _copy(new)

    def find_finalised_run_by_key(self, run_key: str) -> Run | None:
        for run in self._runs.values():
            if run.status is RunStatus.FINALISED and run.run_key == run_key:
                return _copy(run)
        return None

    # --- source files --------------------------------------------------------------------

    def add_source_file(self, file: SourceFile) -> SourceFile:
        run = self._run(file.run_id)
        if run.status is not RunStatus.CREATED:  # as the 0002 insert trigger
            raise StateConflict(_copy(run))
        if file.id in self._files:
            raise UniqueViolation("source_files_pkey")
        key = (file.run_id, file.sha256)
        if key in self._file_keys:
            raise UniqueViolation("source_files_run_sha256_uq")
        self._files[file.id] = _copy(file)
        self._file_keys[key] = file.id
        return _copy(file)

    def list_source_files(self, run_id: UUID, source: Source | None = None) -> list[SourceFile]:
        return [
            _copy(f)
            for f in self._files.values()
            if f.run_id == run_id and (source is None or f.source is source)
        ]

    def _file(self, file_id: UUID) -> SourceFile:
        try:
            return self._files[file_id]
        except KeyError:
            raise NotFound("source_file", file_id) from None

    def confirm_source_file(self, file_id: UUID, at: datetime) -> SourceFile:
        new = _updated(self._file(file_id), {"confirmed_at": at})
        self._files[file_id] = new
        return _copy(new)

    def delete_source_file(self, file_id: UUID) -> None:
        file = self._file(file_id)
        run = self._run(file.run_id)
        if run.status is not RunStatus.CREATED:
            raise StateConflict(_copy(run))
        del self._files[file_id]
        del self._file_keys[(file.run_id, file.sha256)]

    # --- page manifest -------------------------------------------------------------------

    def get_page(self, sha256: str) -> PageManifestEntry | None:
        page = self._pages.get(sha256)
        return None if page is None else _copy(page)

    def add_page_if_new(self, entry: PageManifestEntry) -> tuple[PageManifestEntry, bool]:
        existing = self._pages.get(entry.sha256)
        if existing is not None:
            return _copy(existing), False
        self._run(entry.first_seen_run_id)
        self._pages[entry.sha256] = _copy(entry)
        return _copy(entry), True

    # --- extraction cache ----------------------------------------------------------------

    def get_extraction(
        self, page_sha256: str, variant: ReadVariant, model: str, prompt_version: str
    ) -> Extraction | None:
        found = self._extractions.get((page_sha256, variant, model, prompt_version))
        return None if found is None else _copy(found)

    def put_extraction(self, extraction: Extraction) -> tuple[Extraction, bool]:
        key = (
            extraction.page_sha256,
            extraction.read_variant,
            extraction.model,
            extraction.prompt_version,
        )
        existing = self._extractions.get(key)
        if existing is not None:
            return _copy(existing), False
        if extraction.id in self._extraction_ids:
            raise UniqueViolation("extractions_pkey")
        self._extractions[key] = _copy(extraction)
        self._extraction_ids.add(extraction.id)
        return _copy(extraction), True

    # --- reconciliation ------------------------------------------------------------------

    def replace_reconciliation(self, run_id: UUID, payload: ReconciliationPayload) -> None:
        run = self._run(run_id)
        if run.status is RunStatus.FINALISED:
            raise StateConflict(_copy(run))
        records: list[CanonicalRow | Group | ControlFlag | NormLogEntry] = [
            *payload.rows,
            *payload.groups,
            *payload.flags,
            *payload.norm_log,
        ]
        if any(r.run_id != run_id for r in records):
            raise ValueError("every payload record must belong to the run")
        group_ids = {g.id for g in payload.groups}
        stray = [
            r.source_row_id
            for r in payload.rows
            if r.group_id is not None and r.group_id not in group_ids
        ]
        if stray:
            raise ValueError(f"rows reference groups not in the payload: {stray}")

        keys = _recon_keys(payload.rows, payload.groups, payload.flags, payload.norm_log)
        for constraint, values in keys:  # repeats inside the payload
            if len(set(values)) != len(values):
                raise UniqueViolation(constraint)
        # primary keys held by other runs (this run's previous outputs are replaced)
        for other_id, o in self._recon.items():
            if other_id == run_id:
                continue
            held = _recon_keys(o.rows.values(), o.groups.values(), o.flags, o.norm_log)
            for (constraint, values), (_, taken) in zip(keys, held, strict=True):
                if constraint.endswith("_pkey") and not set(taken).isdisjoint(values):
                    raise UniqueViolation(constraint)

        # build the replacement aside; it becomes visible only once complete (atomic)
        fresh = _Reconciliation()
        fresh.rows.update((r.source_row_id, _copy(r)) for r in payload.rows)
        fresh.groups.update((g.group_key, _copy(g)) for g in payload.groups)
        fresh.flags.extend(_copy(f) for f in payload.flags)
        fresh.norm_log.extend(_copy(n) for n in payload.norm_log)
        self._recon[run_id] = fresh

    def _recon_of(self, run_id: UUID) -> _Reconciliation:
        return self._recon.get(run_id) or _Reconciliation()

    def list_source_rows(self, run_id: UUID, source: Source | None = None) -> list[CanonicalRow]:
        rows = [
            r for r in self._recon_of(run_id).rows.values() if source is None or r.source is source
        ]
        rows.sort(key=lambda r: (_SOURCE_ORDER[r.source], r.source_row_id))
        return [_copy(r) for r in rows]

    def get_source_row(self, run_id: UUID, source_row_id: str) -> CanonicalRow:
        row = self._recon_of(run_id).rows.get(source_row_id)
        if row is None:
            raise NotFound("source_row", source_row_id)
        return _copy(row)

    def list_groups(self, run_id: UUID, outcome: Outcome | None = None) -> list[Group]:
        groups = [
            g
            for g in self._recon_of(run_id).groups.values()
            if outcome is None or g.outcome is outcome
        ]
        groups.sort(key=lambda g: (g.grn_no is None, g.grn_no or 0, g.group_key))
        return [_copy(g) for g in groups]

    def get_group(self, run_id: UUID, group_key: str) -> Group:
        group = self._recon_of(run_id).groups.get(group_key)
        if group is None:
            raise NotFound("group", group_key)
        return _copy(group)

    def list_flags(self, run_id: UUID) -> list[ControlFlag]:
        return [_copy(f) for f in self._recon_of(run_id).flags]

    def list_norm_log(self, run_id: UUID, source_row_id: str | None = None) -> list[NormLogEntry]:
        return [
            _copy(n)
            for n in self._recon_of(run_id).norm_log
            if source_row_id is None or n.source_row_id == source_row_id
        ]

    # --- proof ---------------------------------------------------------------------------

    def save_proof(self, proof: Proof) -> Proof:
        run = self._run(proof.run_id)
        if run.status is RunStatus.FINALISED:
            raise StateConflict(_copy(run))
        self._proofs[proof.run_id] = _copy(proof)
        return _copy(proof)

    def get_proof(self, run_id: UUID) -> Proof | None:
        proof = self._proofs.get(run_id)
        return None if proof is None else _copy(proof)

    # --- review --------------------------------------------------------------------------

    def upsert_review_items(self, run_id: UUID, items: list[ReviewItem]) -> list[ReviewItem]:
        run = self._run(run_id)
        if any(i.run_id != run_id for i in items):
            raise ValueError("every review item must belong to the run")
        # validate the whole batch before writing anything (atomic)
        batch_keys: set[str] = set()
        new_ids: set[UUID] = set()
        for item in items:
            if item.item_key in batch_keys:
                raise UniqueViolation("review_items_run_item_key_uq")
            batch_keys.add(item.item_key)
            if (run_id, item.item_key) not in self._review_keys:
                if item.id in self._reviews or item.id in new_ids:
                    raise UniqueViolation("review_items_pkey")
                new_ids.add(item.id)
            elif run.status is RunStatus.FINALISED:  # migration 0003: no refresh either
                raise StateConflict(_copy(run))

        staged: list[ReviewItem] = []
        for item in items:
            existing_id = self._review_keys.get((run_id, item.item_key))
            if existing_id is None:
                staged.append(
                    _updated(
                        item,
                        {
                            "status": ReviewStatus.OPEN,
                            "resolved_value": None,
                            "resolved_by": None,
                            "resolved_at": None,
                            "note": None,
                        },
                    )
                )
            else:
                refreshed = {name: getattr(item, name) for name in _REVIEW_REFRESHED_FIELDS}
                staged.append(_updated(self._reviews[existing_id], refreshed))
        for new in staged:
            self._reviews[new.id] = new
            self._review_keys[(run_id, new.item_key)] = new.id
        return [_copy(n) for n in staged]

    def list_review_items(
        self, run_id: UUID, status: ReviewStatus | None = None
    ) -> list[ReviewItem]:
        items = [
            i
            for i in self._reviews.values()
            if i.run_id == run_id and (status is None or i.status is status)
        ]
        items.sort(key=_review_sort_key)
        return [_copy(i) for i in items]

    def get_review_item(self, item_id: UUID) -> ReviewItem:
        try:
            return _copy(self._reviews[item_id])
        except KeyError:
            raise NotFound("review_item", item_id) from None

    def resolve_review_item(
        self,
        item_id: UUID,
        *,
        status: ReviewStatus,
        resolved_value: str | None,
        resolved_by: str,
        resolved_at: datetime,
        note: str | None,
    ) -> ReviewItem:
        current = self._reviews.get(item_id)
        if current is None:
            raise NotFound("review_item", item_id)
        if status is ReviewStatus.OPEN:
            raise ValueError("a decision must be RESOLVED or CANNOT_DETERMINE")
        if status is ReviewStatus.CANNOT_DETERMINE and not (note and note.strip()):
            raise ValueError("CANNOT_DETERMINE needs a non-empty note")
        if current.status is not ReviewStatus.OPEN:
            raise StateConflict(_copy(current))
        run = self._run(current.run_id)
        if run.status is RunStatus.FINALISED:  # migration 0003 review_items_refuse_finalised_run
            raise StateConflict(_copy(run))
        new = _updated(
            current,
            {
                "status": status,
                "resolved_value": resolved_value,
                "resolved_by": resolved_by,
                "resolved_at": resolved_at,
                "note": note,
            },
        )
        self._reviews[item_id] = new
        return _copy(new)

    # --- notifications -------------------------------------------------------------------

    def insert_notification(self, n: Notification) -> tuple[Notification, bool]:
        existing_id = self._notification_keys.get(n.idempotency_key)
        if existing_id is not None:
            return _copy(self._notifications[existing_id]), False
        self._run(n.run_id)
        if n.id in self._notifications:
            raise UniqueViolation("notifications_pkey")
        self._notifications[n.id] = _copy(n)
        self._notification_keys[n.idempotency_key] = n.id
        return _copy(n), True

    def get_notification_by_key(self, idempotency_key: str) -> Notification | None:
        found_id = self._notification_keys.get(idempotency_key)
        return None if found_id is None else _copy(self._notifications[found_id])

    def update_notification(
        self, notification_id: UUID, *, expected: set[NotificationStatus], **fields: Any
    ) -> Notification:
        current = self._notifications.get(notification_id)
        if current is None:
            raise NotFound("notification", notification_id)
        bad = _NOTIFICATION_FIXED_FIELDS.intersection(fields)
        if bad:
            raise ValueError(f"update_notification cannot set {sorted(bad)}")
        if fields.get("updated_at") is None:
            raise ValueError("update_notification needs updated_at (caller passes clock time)")
        if current.status not in expected:
            raise StateConflict(_copy(current))
        new = _updated(current, fields)
        self._notifications[notification_id] = new
        return _copy(new)

    def list_notifications(self, run_id: UUID) -> list[Notification]:
        return [_copy(n) for n in self._notifications.values() if n.run_id == run_id]

    def list_stale_sending(self, older_than: datetime) -> list[Notification]:
        return [
            _copy(n)
            for n in self._notifications.values()
            if n.status is NotificationStatus.SENDING and n.updated_at < older_than
        ]

    def get_latest_notification(self, kind: NotificationKind) -> Notification | None:
        found = [n for n in self._notifications.values() if n.kind is kind]
        return _copy(max(found, key=lambda n: (n.updated_at, n.id))) if found else None

    # --- run events ----------------------------------------------------------------------

    def add_run_event(self, event: RunEvent) -> RunEvent:
        self._run(event.run_id)
        if event.id in self._event_ids:
            raise UniqueViolation("run_events_pkey")
        self._events.append(_copy(event))
        self._event_ids.add(event.id)
        return _copy(event)

    def list_run_events(self, run_id: UUID) -> list[RunEvent]:
        # at desc, then newest insertion first (C27: the Postgres `seq` desc)
        events = [(e.at, i, e) for i, e in enumerate(self._events) if e.run_id == run_id]
        events.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return [_copy(e) for _, _, e in events]

    # --- audit ---------------------------------------------------------------------------

    def append_audit(self, entry: AuditEntry) -> AuditEntry:
        stored = _updated(entry, {"id": len(self._audit) + 1})
        self._audit.append(stored)
        return _copy(stored)

    def list_audit(self, run_id: UUID | None = None, actor: str | None = None) -> list[AuditEntry]:
        entries = [  # iterating the log already yields copies
            e
            for e in self._audit
            if (run_id is None or e.run_id == run_id) and (actor is None or e.actor == actor)
        ]
        entries.sort(key=lambda e: (e.at, e.id), reverse=True)
        return entries


def _locked(method: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(method)
    def call(self: MemoryRepo, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return call


for _name, _method in list(vars(MemoryRepo).items()):
    if callable(_method) and not _name.startswith("_"):
        setattr(MemoryRepo, _name, _locked(_method))


def _review_sort_key(item: ReviewItem) -> tuple[int, bool, int, bool, int, str]:
    """F-08.R2: kind order, then page/row from context (missing last), then item_key."""
    page = getattr(item.context, "page_no", None)
    row = getattr(item.context, "row_index", None)
    return (_KIND_ORDER[item.kind], page is None, page or 0, row is None, row or 0, item.item_key)


if TYPE_CHECKING:
    from app.core.db import Repo

    _conforms: Repo = MemoryRepo()
