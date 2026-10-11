"""`Repo` protocol (data-model.md Section 7). Behaviour: contracts/repo-interface.md.

Every method returns copies of models. Errors come from `app.core.errors`: `NotFound`,
`UniqueViolation`, `StateConflict(current)`, `AppendOnlyViolation`, `UpstreamFailed`.
"""

from collections.abc import Sequence
from datetime import datetime
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from uuid import UUID

from app.core.config import get_settings
from app.domain.enums import (
    NotificationKind,
    NotificationStatus,
    Outcome,
    ProofStatus,
    ReadVariant,
    ReviewStatus,
    RunStatus,
    Source,
)
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

if TYPE_CHECKING:
    from app.core.memory_repo import MemoryRepo


@runtime_checkable
class Repo(Protocol):
    # health
    def ping(self) -> bool: ...

    # users (F-12)
    def get_allowed_user(self, email: str) -> AllowedUser | None: ...
    def upsert_allowed_user(self, user: AllowedUser) -> AllowedUser: ...

    # runs (F-01, all stages)
    def create_run(self, run: Run) -> Run: ...
    def get_run(self, run_id: UUID) -> Run:
        """NotFound."""
        ...

    def list_runs(self, limit: int = 20) -> list[Run]:
        """Newest first: created_at desc, then id."""
        ...

    def transition_run(
        self, run_id: UUID, to: RunStatus, *, expected: set[RunStatus], **fields: Any
    ) -> Run:
        """StateConflict; enforces ALLOWED_TRANSITIONS. Extra fields: run_key, error_message,
        failed_stage, finalised_at, finalised_by (else ValueError). FAILED <=> failed_stage set;
        leaving FAILED clears failed_stage and error_message (passing either is ValueError)."""
        ...

    def update_run_fields(self, run_id: UUID, **fields: Any) -> Run:
        """run_key, error_message, failed_stage only (else ValueError); keeps FAILED <=>
        failed_stage set; StateConflict on a FINALISED run."""
        ...

    def find_finalised_run_by_key(self, run_key: str) -> Run | None: ...
    def delete_run(self, run_id: UUID) -> None:
        """NotFound. Removes the run and every row that hangs off it, at any status (the
        delete-run action). The audit log and the extraction cache are kept."""
        ...

    # source files (F-01)
    def add_source_file(self, file: SourceFile) -> SourceFile:
        """UniqueViolation on (run_id, sha256)."""
        ...

    def list_source_files(self, run_id: UUID, source: Source | None = None) -> list[SourceFile]: ...
    def confirm_source_file(self, file_id: UUID, at: datetime) -> SourceFile: ...
    def delete_source_file(self, file_id: UUID) -> None:
        """Only while the run is CREATED (FM-02)."""
        ...

    # page manifest (F-01.R6, F-03)
    def get_page(self, sha256: str) -> PageManifestEntry | None: ...
    def add_page_if_new(self, entry: PageManifestEntry) -> tuple[PageManifestEntry, bool]:
        """(stored, created)."""
        ...

    # extraction cache (F-03.R1)
    def get_extraction(
        self, page_sha256: str, variant: ReadVariant, model: str, prompt_version: str
    ) -> Extraction | None: ...
    def put_extraction(self, extraction: Extraction) -> tuple[Extraction, bool]:
        """Existing entry returned on key conflict; never overwritten."""
        ...

    def list_extractions(
        self, page_sha256s: Sequence[str], model: str, prompt_version: str
    ) -> list[Extraction]:
        """006 (NFR-02): every read of the pages under `model` and `prompt_version`, in one
        read; ordered by page_sha256, then read_variant; [] for no pages."""
        ...

    # reconciliation (F-04 to F-06)
    def replace_reconciliation(self, run_id: UUID, payload: ReconciliationPayload) -> None:
        """Atomic: all or nothing. StateConflict(run) on a FINALISED run; ValueError if a record
        belongs to another run or a row references a group not in the payload."""
        ...

    def list_source_rows(
        self, run_id: UUID, source: Source | None = None
    ) -> list[CanonicalRow]: ...
    def get_source_row(self, run_id: UUID, source_row_id: str) -> CanonicalRow:
        """NotFound."""
        ...

    def list_groups(self, run_id: UUID, outcome: Outcome | None = None) -> list[Group]:
        """grn_no (nulls last), then group_key."""
        ...

    def get_group(self, run_id: UUID, group_key: str) -> Group:
        """NotFound."""
        ...

    def list_flags(self, run_id: UUID) -> list[ControlFlag]: ...
    def list_norm_log(
        self, run_id: UUID, source_row_id: str | None = None
    ) -> list[NormLogEntry]: ...
    def count_outcomes(self, run_ids: Sequence[UUID]) -> dict[UUID, dict[Outcome, int]]:
        """006 (EP-04, NFR-02): the flat `OutcomeCounts` of each run (004 data-model.md §2:
        groups per outcome, rows over all sources for `ROW_ONLY_OUTCOMES`), zero-filled for
        every `Outcome` and every run given, in one read."""
        ...

    # proof (F-07)
    def save_proof(self, proof: Proof) -> Proof:
        """Overwrite. StateConflict(run) on a FINALISED run."""
        ...

    def get_proof(self, run_id: UUID) -> Proof | None: ...
    def list_proof_statuses(self, run_ids: Sequence[UUID]) -> dict[UUID, ProofStatus]:
        """006 (EP-04, NFR-02): the proof status of each run that has a proof, in one read."""
        ...

    # review (F-08)
    def upsert_review_items(self, run_id: UUID, items: list[ReviewItem]) -> list[ReviewItem]:
        """Insert new item_keys as OPEN; refresh existing ones; keep decisions; never delete.
        Atomic: the whole batch is validated before anything is written."""
        ...

    def list_review_items(
        self, run_id: UUID, status: ReviewStatus | None = None
    ) -> list[ReviewItem]: ...
    def get_review_item(self, item_id: UUID) -> ReviewItem:
        """NotFound."""
        ...

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
        """Only if current status is OPEN, else StateConflict(current item) (FM-09)."""
        ...

    # notifications (F-10, F-11.R4)
    def insert_notification(self, n: Notification) -> tuple[Notification, bool]:
        """Existing returned on idempotency_key conflict."""
        ...

    def get_notification_by_key(self, idempotency_key: str) -> Notification | None: ...
    def update_notification(
        self, notification_id: UUID, *, expected: set[NotificationStatus], **fields: Any
    ) -> Notification:
        """StateConflict; `updated_at` must be among the fields."""
        ...

    def list_notifications(self, run_id: UUID) -> list[Notification]: ...
    def list_stale_sending(self, older_than: datetime) -> list[Notification]:
        """F-10.R7: SENDING and updated_at < older_than."""
        ...

    def get_latest_notification(self, kind: NotificationKind) -> Notification | None:
        """004 E2 (F-11.R5): latest of `kind` by updated_at, then id; None if none."""
        ...

    # run events (F-11.R2)
    def add_run_event(self, event: RunEvent) -> RunEvent: ...
    def list_run_events(self, run_id: UUID) -> list[RunEvent]:
        """Newest first."""
        ...

    # audit (F-11.R6); no update or delete methods exist
    def append_audit(self, entry: AuditEntry) -> AuditEntry:
        """id assigned by the store."""
        ...

    def list_audit(self, run_id: UUID | None = None, actor: str | None = None) -> list[AuditEntry]:
        """Newest first; filters are ANDed."""
        ...


@lru_cache(maxsize=1)
def shared_memory_repo() -> "MemoryRepo":
    """The process-wide `MemoryRepo` behind `REPO_BACKEND=memory`; tests clear the cache."""
    from app.core.memory_repo import MemoryRepo

    return MemoryRepo()


@lru_cache(maxsize=1)
def _supabase_repo() -> Repo:
    # imported lazily: the memory backend (tests, local runs) never loads the Supabase client
    from app.core.supabase_repo import SupabaseRepo

    return SupabaseRepo(get_settings())


def get_repo() -> Repo:
    """FastAPI dependency (004 data-model §5): the store chosen by `REPO_BACKEND`."""
    if get_settings().REPO_BACKEND == "memory":
        return shared_memory_repo()
    return _supabase_repo()
