"""`SupabaseRepo`: the `Repo` protocol over Supabase PostgREST (contracts/repo-interface.md,
004 research R12).

Service-role client, created on first use (never logged). Quantities travel as strings both
ways: models are dumped in JSON mode and `source_rows` reads cast `qty`/`qty_base` to text.
Compare-and-set = filtered update on the status read + returned row count (0 rows → re-read →
`StateConflict`). Postgres `23505` → `UniqueViolation(constraint)` or "existing returned";
`23503` (missing parent run) → `NotFound`. `replace_reconciliation` is one `rpc` call (one
transaction, F-05.R10). Transient failures (network, 429/5xx, PostgREST `PGRST000-003`,
Postgres connection/resource/serialization errors) are retried by `retrying("supabase", 3,
(1, 2))` and end in `UpstreamFailed`. List orders follow `MemoryRepo` (G3): `seq` for insertion
order; text keys (`source_row_id`, `group_key`, review order) are sorted in Python so the order
never depends on the database collation.
"""

import logging
import re
import time
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import UUID

from postgrest.exceptions import APIError
from pydantic import BaseModel

from app.core.config import Settings
from app.core.errors import NotFound, StateConflict, UniqueViolation, UpstreamFailed
from app.core.memory_repo import (
    _NOTIFICATION_FIXED_FIELDS,
    _REVIEW_REFRESHED_FIELDS,
    _RUN_TRANSITION_FIELDS,
    _RUN_UPDATABLE_FIELDS,
    _SOURCE_ORDER,
    _review_sort_key,
    _updated,
)
from app.domain.enums import (
    NotificationKind,
    NotificationStatus,
    Outcome,
    ReadVariant,
    ReviewStatus,
    RunStatus,
    Source,
)
from app.domain.lifecycle import can_transition
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
from app.services.runlog import retrying, transient_cause

logger = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)

SERVICE = "supabase"
ATTEMPTS = 3
WAITS_S = (1, 2)
TIMEOUT_S = 10

_CONSTRAINT = re.compile(r'constraint "([^"]+)"|\(([a-z0-9_]+)\)\s*$')
_SOURCE_ROW_COLUMNS = ",".join(
    f"{name}::text" if name in ("qty", "qty_base") else name for name in CanonicalRow.model_fields
)


def supabase_cause(error: BaseException | None) -> str | None:
    """`retrying` cause for Supabase calls (R8): network errors, 429/5xx, PostgREST
    connection codes and Postgres connection/resource/serialization errors."""
    if isinstance(error, APIError):
        code = str(error.code or "")
        if len(code) == 3 and code.isdigit():  # no JSON body: the HTTP status (e.g. 502)
            return _http_cause(int(code))
        if code in ("PGRST000", "PGRST001", "PGRST002", "PGRST003") or code.startswith("08"):
            return "connection error"
        if code.startswith(("53", "57P")) or code in ("40001", "40P01"):
            return "server error"
        return None
    status = getattr(error, "status", None)  # storage3 StorageApiError
    if status is not None and str(status).isdigit():
        return _http_cause(int(status))
    return transient_cause(error)


def _http_cause(status: int) -> str | None:
    return "rate limited" if status == 429 else "server error" if status >= 500 else None


def create_service_client(settings: Settings) -> Any:
    """The supabase client with the service-role key (server side only, never logged)."""
    key = settings.SUPABASE_SERVICE_ROLE_KEY
    if not settings.SUPABASE_URL or key is None or not key.get_secret_value():
        raise UpstreamFailed(SERVICE, "SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY is not set")
    from supabase import create_client
    from supabase.lib.client_options import SyncClientOptions

    options = SyncClientOptions(
        auto_refresh_token=False,
        persist_session=False,
        postgrest_client_timeout=TIMEOUT_S,
        storage_client_timeout=TIMEOUT_S,
    )
    return create_client(settings.SUPABASE_URL, key.get_secret_value(), options=options)


def _constraint(error: APIError) -> str:
    match = _CONSTRAINT.search(error.message or "")
    return (match.group(1) or match.group(2)) if match else "unknown"


def _model(model: type[M], row: dict[str, Any]) -> M:
    """Validate a row into `model`, dropping columns it does not have (e.g. `seq`)."""
    return model.model_validate({k: v for k, v in row.items() if k in model.model_fields})


def _json(model: BaseModel, *, exclude: set[str] | None = None) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude=exclude)


def _values(fields: dict[str, Any], model: BaseModel) -> dict[str, Any]:
    """The JSON values of `fields` as stored in the validated `model`."""
    return {k: v for k, v in _json(model).items() if k in fields}


class SupabaseRepo:
    """`Repo` over PostgREST. `client` and `sleep` are for tests."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._client_obj = client
        self._sleep = sleep

    def __repr__(self) -> str:
        return f"SupabaseRepo(url={self._settings.SUPABASE_URL!r})"

    # --- plumbing ------------------------------------------------------------------------

    def _client(self) -> Any:
        if self._client_obj is None:
            self._client_obj = create_service_client(self._settings)
        return self._client_obj

    def _table(self, name: str) -> Any:
        return self._client().table(name)

    def _send(self, build: Callable[[], Any]) -> Any:
        """Build and execute one query, retrying transient failures; returns `.data`.
        APIError (non-transient) is left to the caller."""
        retry = retrying(SERVICE, ATTEMPTS, WAITS_S, cause_of=supabase_cause, sleep=self._sleep)
        return retry(lambda: build().execute()).data

    def _call(self, build: Callable[[], Any], missing: tuple[str, object] | None = None) -> Any:
        """`_send` with the generic error mapping: 23505 → UniqueViolation, 23503 → NotFound
        (`missing` = the parent entity), anything else → UpstreamFailed."""
        try:
            return self._send(build)
        except APIError as error:
            raise self._mapped(error, missing) from None

    @staticmethod
    def _mapped(error: APIError, missing: tuple[str, object] | None = None) -> Exception:
        if error.code == "23505":
            return UniqueViolation(_constraint(error))
        if error.code == "23503" and missing is not None:
            return NotFound(*missing)
        if error.code == "22023":
            return ValueError(error.message or "invalid parameter")
        logger.warning("supabase rejected a request: code %s", error.code)
        return UpstreamFailed(SERVICE, f"rejected: {error.code}", attempts=1)

    def _rows(self, build: Callable[[], Any], model: type[M]) -> list[M]:
        return [_model(model, r) for r in self._call(build) or []]

    def _one(self, build: Callable[[], Any], model: type[M]) -> M | None:
        rows = self._rows(build, model)
        return rows[0] if rows else None

    # --- health --------------------------------------------------------------------------

    def ping(self) -> bool:
        try:
            self._client().table("allowed_users").select("email").limit(1).execute()
        except Exception as exc:  # EP-01 reports down; never raises
            logger.warning("supabase ping failed: %s", type(exc).__name__)
            return False
        return True

    # --- users ---------------------------------------------------------------------------

    def get_allowed_user(self, email: str) -> AllowedUser | None:
        return self._one(
            lambda: self._table("allowed_users").select("*").eq("email", email), AllowedUser
        )

    def upsert_allowed_user(self, user: AllowedUser) -> AllowedUser:
        rows = self._call(
            lambda: self._table("allowed_users").upsert(_json(user), on_conflict="email")
        )
        return _model(AllowedUser, rows[0])

    # --- runs ----------------------------------------------------------------------------

    def create_run(self, run: Run) -> Run:
        rows = self._call(lambda: self._table("runs").insert(_json(run)))
        return _model(Run, rows[0])

    def _find_run(self, run_id: UUID) -> Run | None:
        return self._one(lambda: self._table("runs").select("*").eq("id", str(run_id)), Run)

    def get_run(self, run_id: UUID) -> Run:
        run = self._find_run(run_id)
        if run is None:
            raise NotFound("run", run_id)
        return run

    def list_runs(self, limit: int = 20) -> list[Run]:
        return self._rows(
            lambda: (
                self._table("runs")
                .select("*")
                .order("created_at", desc=True)
                .order("id")
                .limit(limit)
            ),
            Run,
        )

    def _cas_run(self, current: Run, values: dict[str, Any]) -> Run:
        """Update `values` only while the run still has the status read (compare-and-set)."""
        rows = self._call(
            lambda: (
                self._table("runs")
                .update(values)
                .eq("id", str(current.id))
                .eq("status", current.status.value)
            )
        )
        if not rows:
            raise StateConflict(self.get_run(current.id))
        return _model(Run, rows[0])

    def transition_run(
        self, run_id: UUID, to: RunStatus, *, expected: set[RunStatus], **fields: Any
    ) -> Run:
        current = self.get_run(run_id)
        bad = set(fields) - _RUN_TRANSITION_FIELDS
        if bad:
            raise ValueError(f"transition_run cannot set {sorted(bad)}")
        if current.status not in expected or not can_transition(current.status, to):
            raise StateConflict(current)
        updates = dict(fields)
        if current.status is RunStatus.FAILED:
            passed = {"failed_stage", "error_message"}.intersection(fields)
            if passed:
                raise ValueError(f"leaving FAILED clears {sorted(passed)}; do not pass them")
            updates.update(failed_stage=None, error_message=None)
        updates["status"] = to
        new = _updated(current, updates)  # validates (FAILED <=> failed_stage)
        return self._cas_run(current, _values(updates, new))

    def update_run_fields(self, run_id: UUID, **fields: Any) -> Run:
        current = self.get_run(run_id)
        bad = set(fields) - _RUN_UPDATABLE_FIELDS
        if bad:
            raise ValueError(f"update_run_fields cannot set {sorted(bad)}")
        if current.status is RunStatus.FINALISED:
            raise StateConflict(current)
        new = _updated(current, fields)
        return self._cas_run(current, _values(fields, new))

    def find_finalised_run_by_key(self, run_key: str) -> Run | None:
        return self._one(
            lambda: (
                self._table("runs")
                .select("*")
                .eq("status", RunStatus.FINALISED.value)
                .eq("run_key", run_key)
            ),
            Run,
        )

    # --- source files --------------------------------------------------------------------

    def add_source_file(self, file: SourceFile) -> SourceFile:
        rows = self._call(
            lambda: self._table("source_files").insert(_json(file)), ("run", file.run_id)
        )
        return _model(SourceFile, rows[0])

    def list_source_files(self, run_id: UUID, source: Source | None = None) -> list[SourceFile]:
        def build() -> Any:
            q = self._table("source_files").select("*").eq("run_id", str(run_id))
            if source is not None:
                q = q.eq("source", source.value)
            return q.order("seq")

        return self._rows(build, SourceFile)

    def _file(self, file_id: UUID) -> SourceFile:
        found = self._one(
            lambda: self._table("source_files").select("*").eq("id", str(file_id)), SourceFile
        )
        if found is None:
            raise NotFound("source_file", file_id)
        return found

    def confirm_source_file(self, file_id: UUID, at: datetime) -> SourceFile:
        rows = self._call(
            lambda: (
                self._table("source_files")
                .update({"confirmed_at": at.isoformat()})
                .eq("id", str(file_id))
            )
        )
        if not rows:
            raise NotFound("source_file", file_id)
        return _model(SourceFile, rows[0])

    def delete_source_file(self, file_id: UUID) -> None:
        file = self._file(file_id)
        run = self.get_run(file.run_id)
        if run.status is not RunStatus.CREATED:
            raise StateConflict(run)
        self._call(lambda: self._table("source_files").delete().eq("id", str(file_id)))

    # --- page manifest -------------------------------------------------------------------

    def get_page(self, sha256: str) -> PageManifestEntry | None:
        return self._one(
            lambda: self._table("page_manifest").select("*").eq("sha256", sha256),
            PageManifestEntry,
        )

    def add_page_if_new(self, entry: PageManifestEntry) -> tuple[PageManifestEntry, bool]:
        try:
            rows = self._call(
                lambda: self._table("page_manifest").insert(_json(entry)),
                ("run", entry.first_seen_run_id),
            )
        except UniqueViolation:
            existing = self.get_page(entry.sha256)
            if existing is None:
                raise
            return existing, False
        return _model(PageManifestEntry, rows[0]), True

    # --- extraction cache ----------------------------------------------------------------

    def get_extraction(
        self, page_sha256: str, variant: ReadVariant, model: str, prompt_version: str
    ) -> Extraction | None:
        return self._one(
            lambda: (
                self._table("extractions")
                .select("*")
                .eq("page_sha256", page_sha256)
                .eq("read_variant", variant.value)
                .eq("model", model)
                .eq("prompt_version", prompt_version)
            ),
            Extraction,
        )

    def put_extraction(self, extraction: Extraction) -> tuple[Extraction, bool]:
        try:
            rows = self._call(lambda: self._table("extractions").insert(_json(extraction)))
        except UniqueViolation:
            existing = self.get_extraction(
                extraction.page_sha256,
                extraction.read_variant,
                extraction.model,
                extraction.prompt_version,
            )
            if existing is None:  # the id collided, not the cache key
                raise
            return existing, False
        return _model(Extraction, rows[0]), True

    # --- reconciliation ------------------------------------------------------------------

    def replace_reconciliation(self, run_id: UUID, payload: ReconciliationPayload) -> None:
        params = {"p_run_id": str(run_id), "p_payload": _json(payload)}
        try:
            self._send(lambda: self._client().rpc("replace_reconciliation", params))
        except APIError as error:
            if error.code == "P0002":
                raise NotFound("run", run_id) from None
            if error.code == "55000":
                raise StateConflict(self.get_run(run_id)) from None
            raise self._mapped(error) from None

    def list_source_rows(self, run_id: UUID, source: Source | None = None) -> list[CanonicalRow]:
        def build() -> Any:
            q = self._table("source_rows").select(_SOURCE_ROW_COLUMNS).eq("run_id", str(run_id))
            return q if source is None else q.eq("source", source.value)

        rows = self._rows(build, CanonicalRow)
        rows.sort(key=lambda r: (_SOURCE_ORDER[r.source], r.source_row_id))
        return rows

    def get_source_row(self, run_id: UUID, source_row_id: str) -> CanonicalRow:
        row = self._one(
            lambda: (
                self._table("source_rows")
                .select(_SOURCE_ROW_COLUMNS)
                .eq("run_id", str(run_id))
                .eq("source_row_id", source_row_id)
            ),
            CanonicalRow,
        )
        if row is None:
            raise NotFound("source_row", source_row_id)
        return row

    def list_groups(self, run_id: UUID, outcome: Outcome | None = None) -> list[Group]:
        def build() -> Any:
            q = self._table("groups").select("*").eq("run_id", str(run_id))
            return q if outcome is None else q.eq("outcome", outcome.value)

        groups = self._rows(build, Group)
        groups.sort(key=lambda g: (g.grn_no is None, g.grn_no or 0, g.group_key))
        return groups

    def get_group(self, run_id: UUID, group_key: str) -> Group:
        group = self._one(
            lambda: (
                self._table("groups")
                .select("*")
                .eq("run_id", str(run_id))
                .eq("group_key", group_key)
            ),
            Group,
        )
        if group is None:
            raise NotFound("group", group_key)
        return group

    def list_flags(self, run_id: UUID) -> list[ControlFlag]:
        return self._rows(
            lambda: self._table("control_flags").select("*").eq("run_id", str(run_id)).order("seq"),
            ControlFlag,
        )

    def list_norm_log(self, run_id: UUID, source_row_id: str | None = None) -> list[NormLogEntry]:
        def build() -> Any:
            q = self._table("normalisation_log").select("*").eq("run_id", str(run_id))
            if source_row_id is not None:
                q = q.eq("source_row_id", source_row_id)
            return q.order("seq")

        return self._rows(build, NormLogEntry)

    # --- proof ---------------------------------------------------------------------------

    def save_proof(self, proof: Proof) -> Proof:
        run = self.get_run(proof.run_id)
        if run.status is RunStatus.FINALISED:
            raise StateConflict(run)
        rows = self._call(
            lambda: self._table("proofs").upsert(_json(proof), on_conflict="run_id"),
            ("run", proof.run_id),
        )
        return _model(Proof, rows[0])

    def get_proof(self, run_id: UUID) -> Proof | None:
        return self._one(lambda: self._table("proofs").select("*").eq("run_id", str(run_id)), Proof)

    # --- review --------------------------------------------------------------------------

    def upsert_review_items(self, run_id: UUID, items: list[ReviewItem]) -> list[ReviewItem]:
        """New item_keys are inserted OPEN in one statement (all or nothing; a used id fails
        it with 23505), then each existing item gets only the refreshed columns, so a decision
        made meanwhile is never overwritten. Validation precedes every write."""
        self.get_run(run_id)
        if any(i.run_id != run_id for i in items):
            raise ValueError("every review item must belong to the run")
        keys = [i.item_key for i in items]
        if len(set(keys)) != len(keys):
            raise UniqueViolation("review_items_run_item_key_uq")
        stored = {
            r.item_key: r
            for r in self._rows(
                lambda: self._table("review_items").select("*").eq("run_id", str(run_id)),
                ReviewItem,
            )
        }
        fresh = [
            _updated(
                i,
                {
                    "status": ReviewStatus.OPEN,
                    "resolved_value": None,
                    "resolved_by": None,
                    "resolved_at": None,
                    "note": None,
                },
            )
            for i in items
            if i.item_key not in stored
        ]
        new_ids = [i.id for i in fresh]
        if len(set(new_ids)) != len(new_ids):
            raise UniqueViolation("review_items_pkey")
        result: dict[str, ReviewItem] = {}
        if fresh:
            rows = self._call(
                lambda: self._table("review_items").insert([_json(i) for i in fresh]),
                ("run", run_id),
            )
            result.update((r.item_key, r) for r in (_model(ReviewItem, row) for row in rows))
        for item in items:
            existing = stored.get(item.item_key)
            if existing is None:
                continue
            refreshed = _updated(
                existing, {name: getattr(item, name) for name in _REVIEW_REFRESHED_FIELDS}
            )
            values = _values(dict.fromkeys(_REVIEW_REFRESHED_FIELDS), refreshed)
            result[item.item_key] = self._refresh_review(existing.id, values) or refreshed
        return [result[k] for k in keys]

    def _refresh_review(self, item_id: UUID, values: dict[str, Any]) -> ReviewItem | None:
        return self._one(
            lambda: self._table("review_items").update(values).eq("id", str(item_id)), ReviewItem
        )

    def list_review_items(
        self, run_id: UUID, status: ReviewStatus | None = None
    ) -> list[ReviewItem]:
        def build() -> Any:
            q = self._table("review_items").select("*").eq("run_id", str(run_id))
            return q if status is None else q.eq("status", status.value)

        items = self._rows(build, ReviewItem)
        items.sort(key=_review_sort_key)
        return items

    def get_review_item(self, item_id: UUID) -> ReviewItem:
        item = self._one(
            lambda: self._table("review_items").select("*").eq("id", str(item_id)), ReviewItem
        )
        if item is None:
            raise NotFound("review_item", item_id)
        return item

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
        current = self.get_review_item(item_id)
        if status is ReviewStatus.OPEN:
            raise ValueError("a decision must be RESOLVED or CANNOT_DETERMINE")
        if status is ReviewStatus.CANNOT_DETERMINE and not (note and note.strip()):
            raise ValueError("CANNOT_DETERMINE needs a non-empty note")
        if current.status is not ReviewStatus.OPEN:
            raise StateConflict(current)
        decision = {
            "status": status,
            "resolved_value": resolved_value,
            "resolved_by": resolved_by,
            "resolved_at": resolved_at,
            "note": note,
        }
        new = _updated(current, decision)
        rows = self._call(
            lambda: (
                self._table("review_items")
                .update(_values(decision, new))
                .eq("id", str(item_id))
                .eq("status", ReviewStatus.OPEN.value)
            )
        )
        if not rows:
            raise StateConflict(self.get_review_item(item_id))
        return _model(ReviewItem, rows[0])

    # --- notifications -------------------------------------------------------------------

    def insert_notification(self, n: Notification) -> tuple[Notification, bool]:
        try:
            rows = self._call(
                lambda: self._table("notifications").insert(_json(n)), ("run", n.run_id)
            )
        except UniqueViolation:
            existing = self.get_notification_by_key(n.idempotency_key)
            if existing is None:
                raise
            return existing, False
        return _model(Notification, rows[0]), True

    def get_notification_by_key(self, idempotency_key: str) -> Notification | None:
        return self._one(
            lambda: self._table("notifications").select("*").eq("idempotency_key", idempotency_key),
            Notification,
        )

    def _notification(self, notification_id: UUID) -> Notification:
        found = self._one(
            lambda: self._table("notifications").select("*").eq("id", str(notification_id)),
            Notification,
        )
        if found is None:
            raise NotFound("notification", notification_id)
        return found

    def update_notification(
        self, notification_id: UUID, *, expected: set[NotificationStatus], **fields: Any
    ) -> Notification:
        current = self._notification(notification_id)
        bad = _NOTIFICATION_FIXED_FIELDS.intersection(fields)
        if bad:
            raise ValueError(f"update_notification cannot set {sorted(bad)}")
        if fields.get("updated_at") is None:
            raise ValueError("update_notification needs updated_at (caller passes clock time)")
        if current.status not in expected:
            raise StateConflict(current)
        new = _updated(current, fields)
        rows = self._call(
            lambda: (
                self._table("notifications")
                .update(_values(fields, new))
                .eq("id", str(notification_id))
                .eq("status", current.status.value)
            )
        )
        if not rows:
            raise StateConflict(self._notification(notification_id))
        return _model(Notification, rows[0])

    def list_notifications(self, run_id: UUID) -> list[Notification]:
        return self._rows(
            lambda: self._table("notifications").select("*").eq("run_id", str(run_id)).order("seq"),
            Notification,
        )

    def list_stale_sending(self, older_than: datetime) -> list[Notification]:
        return self._rows(
            lambda: (
                self._table("notifications")
                .select("*")
                .eq("status", NotificationStatus.SENDING.value)
                .lt("updated_at", older_than.isoformat())
                .order("seq")
            ),
            Notification,
        )

    def get_latest_notification(self, kind: NotificationKind) -> Notification | None:
        return self._one(
            lambda: (
                self._table("notifications")
                .select("*")
                .eq("kind", kind.value)
                .order("updated_at", desc=True)
                .order("id", desc=True)
                .limit(1)
            ),
            Notification,
        )

    # --- run events ----------------------------------------------------------------------

    def add_run_event(self, event: RunEvent) -> RunEvent:
        rows = self._call(
            lambda: self._table("run_events").insert(_json(event)), ("run", event.run_id)
        )
        return _model(RunEvent, rows[0])

    def list_run_events(self, run_id: UUID) -> list[RunEvent]:
        """Newest first: `at` desc, then `seq` desc (C27: newest insertion first on ties)."""
        return self._rows(
            lambda: (
                self._table("run_events")
                .select("*")
                .eq("run_id", str(run_id))
                .order("at", desc=True)
                .order("seq", desc=True)
            ),
            RunEvent,
        )

    # --- audit (append-only: insert only; the database trigger refuses update/delete) ----

    def append_audit(self, entry: AuditEntry) -> AuditEntry:
        rows = self._call(lambda: self._table("audit_log").insert(_json(entry, exclude={"id"})))
        return _model(AuditEntry, rows[0])

    def list_audit(self, run_id: UUID | None = None, actor: str | None = None) -> list[AuditEntry]:
        def build() -> Any:
            q = self._table("audit_log").select("*")
            if run_id is not None:
                q = q.eq("run_id", str(run_id))
            if actor is not None:
                q = q.eq("actor", actor)
            return q.order("at", desc=True).order("id", desc=True)

        return self._rows(build, AuditEntry)


if TYPE_CHECKING:  # static conformance check (mypy)
    from app.core.db import Repo

    _conforms: Repo = SupabaseRepo(Settings())
