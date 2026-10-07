"""`SupabaseRepo`: the `Repo` protocol over Supabase PostgREST (contracts/repo-interface.md,
004 research R12).

Service-role client, created on first use (never logged). Quantities travel as strings both
ways: models are dumped in JSON mode and `source_rows` reads cast `qty`/`qty_base` to text.
Compare-and-set = filtered update on the status read + returned row count (0 rows → re-read →
`StateConflict`). Postgres `23505` → `UniqueViolation(constraint)` or "existing returned";
`23503` (missing parent run) → `NotFound`; `55000` (run state, from the RPCs and the 0002 and
0003 triggers) → `StateConflict(run)`. `replace_reconciliation` and `upsert_review_items` are one
`rpc` call each (one transaction). Transient failures (network, 429/5xx, PostgREST
`PGRST000-003`, Postgres connection/resource/serialization errors) are retried by
`retrying("supabase", 3, (1, 2))` and end in `UpstreamFailed`.

A retried write may have landed on an earlier attempt. Only then (attempts > 1): a primary-key
`23505` whose stored row equals ours is success, and a CAS matching 0 rows whose stored values
equal the intended update is success. `append_audit` (no natural key) retries only errors raised
before the request reached the server.

Lists page past the PostgREST row cap with `.range()` over a total order (G3: `seq`, or `id`).
Text keys (`source_row_id`, `group_key`, review order) are sorted in Python so the order never
depends on the database collation.
"""

import logging
import re
import time
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import UUID

import httpx
from postgrest.exceptions import APIError
from pydantic import BaseModel

from app.core.config import Settings
from app.core.errors import NotFound, StateConflict, UniqueViolation, UpstreamFailed
from app.core.memory_repo import (
    _NOTIFICATION_FIXED_FIELDS,
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
from app.services.runlog import CauseOf, retrying, transient_cause

logger = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)
T = TypeVar("T")

SERVICE = "supabase"
ATTEMPTS = 3
WAITS_S = (1, 2)
TIMEOUT_S = 10
PAGE_ROWS = 1000
"""PostgREST's default `max-rows`; lists read pages of up to this size until an empty one."""
REVIEW_CHUNK = 500
"""Review items per `upsert_review_items` RPC call (bounded request size)."""

_CONNECT_CODES = ("PGRST000", "PGRST001", "PGRST002", "PGRST003")
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
        if code in _CONNECT_CODES or code.startswith("08"):
            return "connection error"
        if code.startswith(("53", "57P")) or code in ("40001", "40P01"):
            return "server error"
        return None
    status = getattr(error, "status", None)  # storage3 StorageApiError
    if status is not None and str(status).isdigit():
        return _http_cause(int(status))
    return transient_cause(error)


def before_server_cause(error: BaseException | None) -> str | None:
    """Retry cause for writes that must not repeat (`append_audit`): only failures that
    happened before the request reached the database."""
    if isinstance(error, httpx.ConnectTimeout):
        return "timeout"
    if isinstance(error, httpx.ConnectError):
        return "connection error"
    if isinstance(error, APIError) and error.code in _CONNECT_CODES:
        return "connection error"
    return None


def _http_cause(status: int) -> str | None:
    return "rate limited" if status == 429 else "server error" if status >= 500 else None


def send_counted(
    fn: Callable[[], T],
    *,
    sleep: Callable[[float], None],
    cause_of: CauseOf = supabase_cause,
) -> tuple[T, int]:
    """`fn` under the Supabase retry policy; returns (result, attempts made). An error raised
    carries the attempts in `attempts_of(error)`."""
    made = 0

    def attempt() -> T:
        nonlocal made
        made += 1
        return fn()

    retry = retrying(SERVICE, ATTEMPTS, WAITS_S, cause_of=cause_of, sleep=sleep)
    try:
        return retry(attempt), made
    except Exception as error:
        error.ledgerline_attempts = made  # type: ignore[attr-defined]
        raise


def attempts_of(error: BaseException) -> int:
    return int(getattr(error, "ledgerline_attempts", 1))


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


def _applied(stored: BaseModel, new: BaseModel, fields: dict[str, Any]) -> bool:
    """Whether `stored` already holds the values of `fields` in `new` (compared as models,
    so time zones do not matter)."""
    return all(getattr(stored, k) == getattr(new, k) for k in fields)


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

    def _send(
        self, build: Callable[[], Any], cause_of: CauseOf = supabase_cause
    ) -> tuple[Any, int]:
        """Build and execute one query under the retry policy: (`.data`, attempts).
        A non-transient APIError is left to the caller (see `attempts_of`)."""
        response, made = send_counted(
            lambda: build().execute(), sleep=self._sleep, cause_of=cause_of
        )
        return response.data, made

    def _call(self, build: Callable[[], Any], missing: tuple[str, object] | None = None) -> Any:
        """`_send` with the generic error mapping: 23505 → UniqueViolation, 23503 → NotFound
        (`missing` = the parent entity), 22023 → ValueError, anything else → UpstreamFailed."""
        try:
            return self._send(build)[0]
        except APIError as error:
            raise self._mapped(error, missing) from None

    @staticmethod
    def _mapped(error: APIError, missing: tuple[str, object] | None = None) -> Exception:
        if error.code == "23505":
            return UniqueViolation(_constraint(error))
        if error.code in ("23503", "P0002") and missing is not None:
            return NotFound(*missing)
        if error.code == "22023":
            return ValueError(error.message or "invalid parameter")
        logger.warning("supabase rejected a request: code %s", error.code)
        return UpstreamFailed(SERVICE, f"rejected: {error.code}", attempts=1)

    def _rows(self, build: Callable[[], Any], model: type[M]) -> list[M]:
        return [_model(model, r) for r in self._call(build) or []]

    def _rows_all(
        self, build: Callable[[], Any], model: type[M], limit: int | None = None
    ) -> list[M]:
        """Every row of `build` (which must end in a total order), up to `PAGE_ROWS` per
        request. Only an empty page ends the list: a project with a lower `max-rows` answers
        with short pages, which must not be taken for the end."""
        out: list[M] = []
        while limit is None or len(out) < limit:
            start = len(out)
            size = PAGE_ROWS if limit is None else min(PAGE_ROWS, limit - start)

            def page_query(first: int = start, n: int = size) -> Any:
                return build().range(first, first + n - 1)

            page = self._rows(page_query, model)
            if not page:
                break
            out.extend(page)
        return out

    def _one(self, build: Callable[[], Any], model: type[M]) -> M | None:
        rows = self._rows(build, model)
        return rows[0] if rows else None

    def _insert(
        self,
        table: str,
        item: M,
        missing: tuple[str, object] | None = None,
        key: str = "id",
        *,
        guarded_run: UUID | None = None,
    ) -> M:
        """Insert `item`. A retried insert that already landed (primary-key 23505 and the
        stored row equals `item`) counts as success. With `guarded_run`, a 0002 trigger's
        55000 is `StateConflict(run)`."""
        try:
            rows = self._send(lambda: self._table(table).insert(_json(item)))[0]
        except APIError as error:
            if error.code == "23505" and attempts_of(error) > 1:
                stored = self._one(
                    lambda: self._table(table).select("*").eq(key, str(getattr(item, key))),
                    type(item),
                )
                if stored == item:
                    return stored
            if guarded_run is not None:
                raise self._run_conflict(error, guarded_run) from None
            raise self._mapped(error, missing) from None
        return _model(type(item), rows[0])

    def _insert_or_existing(
        self,
        table: str,
        item: M,
        existing: Callable[[], M | None],
        ours: Callable[[M], bool],
        missing: tuple[str, object],
    ) -> tuple[M, bool]:
        """ "Existing returned" insert: on 23505 return (existing, False), or (existing, True)
        when a retry hit our own earlier insert (`ours`)."""
        try:
            rows = self._send(lambda: self._table(table).insert(_json(item)))[0]
        except APIError as error:
            if error.code != "23505":
                raise self._mapped(error, missing) from None
            found = existing()
            if found is None:  # the id collided, not the natural key
                raise self._mapped(error, missing) from None
            return found, attempts_of(error) > 1 and ours(found)
        return _model(type(item), rows[0]), True

    def _cas(
        self,
        table: str,
        current: M,
        new: M,
        fields: dict[str, Any],
        guard: tuple[str, str],
        reread: Callable[[], M],
        *,
        guarded_run: UUID | None = None,
    ) -> M:
        """Update `fields` (values from `new`) only while `guard` (column, value) still holds.
        0 rows → `StateConflict(reread())`, unless a retry already applied the update. With
        `guarded_run`, a trigger's 55000 is `StateConflict(run)`."""
        values = _values(fields, new)
        row_id = str(_json(current)["id"])  # M is any model with an id

        def build() -> Any:
            return self._table(table).update(values).eq("id", row_id).eq(*guard)

        if guarded_run is None:
            rows, made = self._send_mapped(build)
        else:
            try:
                rows, made = self._send(build)
            except APIError as error:
                raise self._run_conflict(error, guarded_run) from None
        if rows:
            return _model(type(current), rows[0])
        stored = reread()
        if made > 1 and _applied(stored, new, fields):
            return stored
        raise StateConflict(stored)

    def _send_mapped(self, build: Callable[[], Any]) -> tuple[Any, int]:
        try:
            return self._send(build)
        except APIError as error:
            raise self._mapped(error) from None

    def _run_conflict(self, error: APIError, run_id: UUID) -> Exception:
        """55000 (run state, raised by the RPCs and 0002 triggers) → StateConflict(run)."""
        if error.code == "55000":
            return StateConflict(self.get_run(run_id))
        return self._mapped(error, ("run", run_id))

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
        return self._insert("runs", run)

    def _find_run(self, run_id: UUID) -> Run | None:
        return self._one(lambda: self._table("runs").select("*").eq("id", str(run_id)), Run)

    def get_run(self, run_id: UUID) -> Run:
        run = self._find_run(run_id)
        if run is None:
            raise NotFound("run", run_id)
        return run

    def list_runs(self, limit: int = 20) -> list[Run]:
        return self._rows_all(
            lambda: self._table("runs").select("*").order("created_at", desc=True).order("id"),
            Run,
            limit,
        )

    def _cas_run(self, current: Run, new: Run, fields: dict[str, Any]) -> Run:
        """Compare-and-set on the status read."""
        return self._cas(
            "runs",
            current,
            new,
            fields,
            ("status", current.status.value),
            lambda: self.get_run(current.id),
        )

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
        return self._cas_run(current, new, updates)

    def update_run_fields(self, run_id: UUID, **fields: Any) -> Run:
        current = self.get_run(run_id)
        bad = set(fields) - _RUN_UPDATABLE_FIELDS
        if bad:
            raise ValueError(f"update_run_fields cannot set {sorted(bad)}")
        if current.status is RunStatus.FINALISED:
            raise StateConflict(current)
        return self._cas_run(current, _updated(current, fields), fields)

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
        """Only while the run is CREATED: the 0002 trigger refuses otherwise (55000)."""
        return self._insert("source_files", file, ("run", file.run_id), guarded_run=file.run_id)

    def list_source_files(self, run_id: UUID, source: Source | None = None) -> list[SourceFile]:
        def build() -> Any:
            q = self._table("source_files").select("*").eq("run_id", str(run_id))
            if source is not None:
                q = q.eq("source", source.value)
            return q.order("seq")

        return self._rows_all(build, SourceFile)

    def _file(self, file_id: UUID) -> SourceFile:
        found = self._one(
            lambda: self._table("source_files").select("*").eq("id", str(file_id)), SourceFile
        )
        if found is None:
            raise NotFound("source_file", file_id)
        return found

    def confirm_source_file(self, file_id: UUID, at: datetime) -> SourceFile:
        new = _updated(self._file(file_id), {"confirmed_at": at})  # rejects a naive time
        rows = self._call(
            lambda: (
                self._table("source_files")
                .update(_values({"confirmed_at": at}, new))
                .eq("id", str(file_id))
            )
        )
        if not rows:
            raise NotFound("source_file", file_id)
        return _model(SourceFile, rows[0])

    def delete_source_file(self, file_id: UUID) -> None:
        """Only while the run is CREATED: the 0002 trigger refuses otherwise (55000)."""
        file = self._file(file_id)
        try:
            self._send(lambda: self._table("source_files").delete().eq("id", str(file_id)))
        except APIError as error:
            raise self._run_conflict(error, file.run_id) from None

    # --- page manifest -------------------------------------------------------------------

    def get_page(self, sha256: str) -> PageManifestEntry | None:
        return self._one(
            lambda: self._table("page_manifest").select("*").eq("sha256", sha256),
            PageManifestEntry,
        )

    def add_page_if_new(self, entry: PageManifestEntry) -> tuple[PageManifestEntry, bool]:
        return self._insert_or_existing(
            "page_manifest",
            entry,
            lambda: self.get_page(entry.sha256),
            lambda found: found == entry,
            ("run", entry.first_seen_run_id),
        )

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
        e = extraction
        return self._insert_or_existing(
            "extractions",
            e,
            lambda: self.get_extraction(e.page_sha256, e.read_variant, e.model, e.prompt_version),
            lambda found: found.id == e.id,
            ("extraction", e.id),
        )

    # --- reconciliation ------------------------------------------------------------------

    def replace_reconciliation(self, run_id: UUID, payload: ReconciliationPayload) -> None:
        params = {"p_run_id": str(run_id), "p_payload": _json(payload)}
        try:
            self._send(lambda: self._client().rpc("replace_reconciliation", params))
        except APIError as error:
            raise self._run_conflict(error, run_id) from None

    def list_source_rows(self, run_id: UUID, source: Source | None = None) -> list[CanonicalRow]:
        def build() -> Any:
            q = self._table("source_rows").select(_SOURCE_ROW_COLUMNS).eq("run_id", str(run_id))
            return (q if source is None else q.eq("source", source.value)).order("id")

        rows = self._rows_all(build, CanonicalRow)
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
            return (q if outcome is None else q.eq("outcome", outcome.value)).order("id")

        groups = self._rows_all(build, Group)
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
        return self._rows_all(
            lambda: self._table("control_flags").select("*").eq("run_id", str(run_id)).order("seq"),
            ControlFlag,
        )

    def list_norm_log(self, run_id: UUID, source_row_id: str | None = None) -> list[NormLogEntry]:
        def build() -> Any:
            q = self._table("normalisation_log").select("*").eq("run_id", str(run_id))
            if source_row_id is not None:
                q = q.eq("source_row_id", source_row_id)
            return q.order("seq")

        return self._rows_all(build, NormLogEntry)

    # --- proof ---------------------------------------------------------------------------

    def save_proof(self, proof: Proof) -> Proof:
        """Upsert; the 0002 trigger refuses a FINALISED run (55000 → StateConflict)."""
        try:
            rows = self._send(
                lambda: self._table("proofs").upsert(_json(proof), on_conflict="run_id")
            )[0]
        except APIError as error:
            raise self._run_conflict(error, proof.run_id) from None
        return _model(Proof, rows[0])

    def get_proof(self, run_id: UUID) -> Proof | None:
        return self._one(lambda: self._table("proofs").select("*").eq("run_id", str(run_id)), Proof)

    # --- review --------------------------------------------------------------------------

    def upsert_review_items(self, run_id: UUID, items: list[ReviewItem]) -> list[ReviewItem]:
        """`upsert_review_items` RPC (migration 0002) per chunk of `REVIEW_CHUNK` items, each one
        transaction: new item_keys are inserted OPEN; existing ones get only the refreshed
        columns, so decisions are kept. Validation of the whole batch precedes every call."""
        self.get_run(run_id)
        if any(i.run_id != run_id for i in items):
            raise ValueError("every review item must belong to the run")
        keys = [i.item_key for i in items]
        if len(set(keys)) != len(keys):
            raise UniqueViolation("review_items_run_item_key_uq")
        stored: dict[str, ReviewItem] = {}
        for start in range(0, len(items), REVIEW_CHUNK):
            chunk = items[start : start + REVIEW_CHUNK]
            params = {"p_run_id": str(run_id), "p_items": [_json(i) for i in chunk]}

            def call(p: dict[str, Any] = params) -> Any:
                return self._client().rpc("upsert_review_items", p)

            try:
                rows = self._send(call)[0]
            except APIError as error:
                raise self._run_conflict(error, run_id) from None
            stored.update((r.item_key, r) for r in (_model(ReviewItem, row) for row in rows or []))
        missing = [k for k in keys if k not in stored]
        if missing:
            raise UpstreamFailed(SERVICE, f"upsert_review_items returned no row for {missing[:3]}")
        return [stored[k] for k in keys]

    def list_review_items(
        self, run_id: UUID, status: ReviewStatus | None = None
    ) -> list[ReviewItem]:
        def build() -> Any:
            q = self._table("review_items").select("*").eq("run_id", str(run_id))
            return (q if status is None else q.eq("status", status.value)).order("id")

        items = self._rows_all(build, ReviewItem)
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
        return self._cas(
            "review_items",
            current,
            _updated(current, decision),
            decision,
            ("status", ReviewStatus.OPEN.value),
            lambda: self.get_review_item(item_id),
            guarded_run=current.run_id,  # migration 0003: no decision on a FINALISED run
        )

    # --- notifications -------------------------------------------------------------------

    def insert_notification(self, n: Notification) -> tuple[Notification, bool]:
        return self._insert_or_existing(
            "notifications",
            n,
            lambda: self.get_notification_by_key(n.idempotency_key),
            lambda found: found.id == n.id,
            ("run", n.run_id),
        )

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
        return self._cas(
            "notifications",
            current,
            _updated(current, fields),
            fields,
            ("status", current.status.value),
            lambda: self._notification(notification_id),
        )

    def list_notifications(self, run_id: UUID) -> list[Notification]:
        return self._rows_all(
            lambda: self._table("notifications").select("*").eq("run_id", str(run_id)).order("seq"),
            Notification,
        )

    def list_stale_sending(self, older_than: datetime) -> list[Notification]:
        return self._rows_all(
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
        return self._insert("run_events", event, ("run", event.run_id))

    def list_run_events(self, run_id: UUID) -> list[RunEvent]:
        """Newest first: `at` desc, then newest insertion first (`seq` desc, C27)."""
        return self._rows_all(
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
        """No natural key to detect a repeat, so only pre-server failures are retried."""
        row = _json(entry, exclude={"id"})
        try:
            rows = self._send(
                lambda: self._table("audit_log").insert(row), cause_of=before_server_cause
            )[0]
        except APIError as error:
            raise self._mapped(error) from None
        except httpx.HTTPError as error:
            raise UpstreamFailed(
                SERVICE, f"{type(error).__name__} (not retried)", attempts=attempts_of(error)
            ) from None
        return _model(AuditEntry, rows[0])

    def list_audit(self, run_id: UUID | None = None, actor: str | None = None) -> list[AuditEntry]:
        def build() -> Any:
            q = self._table("audit_log").select("*")
            if run_id is not None:
                q = q.eq("run_id", str(run_id))
            if actor is not None:
                q = q.eq("actor", actor)
            return q.order("at", desc=True).order("id", desc=True)

        return self._rows_all(build, AuditEntry)


if TYPE_CHECKING:  # static conformance check (mypy)
    from app.core.db import Repo

    _conforms: Repo = SupabaseRepo(Settings())
