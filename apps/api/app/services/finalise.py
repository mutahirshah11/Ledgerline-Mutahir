"""Run overview data and finalise (005 plan "Complexity Tracking", key point 2; data-model.md
§2, §6 to §8, research R8, R9, R15; F-09, F-10, SCR-04).

`load_run_view` is the EP-05 body and the `run` of EP-15 and EP-17: the Phase 3 `RunDetail`
(Run fields, stage progress, counts, proof status, open review items) plus row counts, the
headline, the exception list and the summary notification (C25, FR-020). `finalise` is EP-17.
"""

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, NoReturn
from uuid import UUID, uuid4

from app.core import clock
from app.core.config import Settings, get_settings
from app.core.db import Repo
from app.core.errors import (
    ApiError,
    ConfirmRequired,
    LedgerlineError,
    StateConflict,
    UniqueViolation,
    UpstreamFailed,
)
from app.core.storage import REPORTS_BUCKET, BlobStore
from app.domain.enums import (
    AuditAction,
    AuditChannel,
    ErrorCode,
    NotificationKind,
    NotificationStatus,
    Outcome,
    ProofStatus,
    ReadVariant,
    Role,
    RunEventStatus,
    RunStatus,
    Source,
    Stage,
)
from app.domain.ids import summary_key
from app.domain.lifecycle import unreconciled_decisions
from app.domain.models import (
    AllowedUser,
    AuditEntry,
    CanonicalRow,
    ControlFlag,
    ExceptionItem,
    Extraction,
    FinaliseRequest,
    FinaliseResponse,
    Group,
    NormLogEntry,
    Notification,
    PageProgress,
    Proof,
    ReadCost,
    ReportLinks,
    ReviewItem,
    Run,
    RunEvent,
    RunView,
    SourceFile,
    StageProgress,
    StageProgressView,
    StageState,
)
from app.pipeline import cost, report
from app.pipeline.engine import open_review_state, outcome_counts_of
from app.services import telegram
from app.services.alerts import stage_failed_alert
from app.services.audit import audit
from app.services.runlog import stage_step
from app.services.telegram import Notifier

# --- stage progress (004 data-model.md §2 `StageProgressView`) -----------------------------

PROGRESS_STAGES = (Stage.UPLOAD, Stage.VALIDATE, Stage.EXTRACT, Stage.RECONCILE, Stage.PROOF)
_STATE: dict[RunEventStatus, StageState] = {
    RunEventStatus.STARTED: "RUNNING",
    RunEventStatus.RETRY: "RUNNING",
    RunEventStatus.OK: "OK",
    RunEventStatus.FAILED: "FAILED",
}


def _extract_cached(events: list[RunEvent]) -> dict[int, bool]:
    """`detail.cached` of the newest extract OK event per page (EP-09 writes page_no, cached)."""
    found: dict[int, bool] = {}
    for e in events:  # newest first
        page_no, cached = e.detail.get("page_no"), e.detail.get("cached")
        if e.stage is Stage.EXTRACT and e.status is RunEventStatus.OK and page_no not in found:
            if isinstance(page_no, int) and isinstance(cached, bool):
                found[page_no] = cached
    return found


def _pages(
    repo: Repo, run: Run, events: list[RunEvent], settings: Settings
) -> tuple[list[PageProgress], list[Extraction]]:
    """`StageProgressView.pages` and the reads this run made new (006 `ReadCost`: the stored
    reads of each page not `cached`, created since the run was). `extracted`: both reads under
    the current model and prompt. `cached`: both reads existed before this run's extract stage
    first touched the page, read from the run's extract OK event; without one, both reads are
    older than the run itself. Every page's reads come from one `list_extractions` call (006,
    NFR-02)."""
    from_events = _extract_cached(events)
    out: list[PageProgress] = []
    new_reads: list[Extraction] = []
    files = sorted(repo.list_source_files(run.id, Source.REGISTER), key=lambda f: f.page_no or 0)
    stored = {
        (e.page_sha256, e.read_variant): e
        for e in repo.list_extractions(
            tuple(f.sha256 for f in files), settings.EXTRACT_MODEL, settings.PROMPT_VERSION
        )
    }
    for f in files:
        page_no = f.page_no or 0
        reads = [stored.get((f.sha256, v)) for v in ReadVariant]
        extracted = all(r is not None for r in reads)
        if page_no in from_events:
            cached = extracted and from_events[page_no]
        else:
            cached = extracted and all(
                r is not None and r.created_at < run.created_at for r in reads
            )
        out.append(PageProgress(page_no=page_no, extracted=extracted, cached=cached))
        if not cached:
            new_reads += [r for r in reads if r is not None and r.created_at >= run.created_at]
    return out, new_reads


def _read_cost(
    pages: list[PageProgress], new_reads: list[Extraction], settings: Settings
) -> ReadCost:
    """EP-05 `read_cost` (`ReadCost` has the rules)."""
    new_pages = sum(not p.cached for p in pages)
    return ReadCost(
        new_pages=new_pages,
        estimate_usd=cost.estimate_read_cost(new_pages, settings),
        actual_usd=cost.read_cost_of(new_reads, settings),
        input_tokens=sum(r.input_tokens for r in new_reads),
        output_tokens=sum(r.output_tokens for r in new_reads),
    )


def stage_progress(repo: Repo, run: Run) -> StageProgressView:
    """Per stage: the state of its newest event (`list_run_events` is newest first); extract
    stays `RUNNING` until every page is read."""
    return progress_and_cost(repo, run)[0]


def progress_and_cost(repo: Repo, run: Run) -> tuple[StageProgressView, ReadCost]:
    """`stage_progress` and the run's `read_cost`, from one read of events and page reads."""
    settings = get_settings()
    events = repo.list_run_events(run.id)
    pages, new_reads = _pages(repo, run, events, settings)
    out: list[StageProgress] = []
    for stage in PROGRESS_STAGES:
        last = next((e for e in events if e.stage is stage), None)
        if last is None:
            out.append(StageProgress(stage=stage, state="PENDING", at=None, duration_ms=None))
            continue
        state = _STATE[last.status]
        if stage is Stage.EXTRACT and state == "OK" and not all(p.extracted for p in pages):
            state = "RUNNING"  # one page read; more to go
        out.append(
            StageProgress(stage=stage, state=state, at=last.at, duration_ms=last.duration_ms)
        )
    return StageProgressView(stages=out, pages=pages), _read_cost(pages, new_reads, settings)


# --- run view (FR-020) ---------------------------------------------------------------------


@dataclass(frozen=True)
class RunData:
    """One read of what the run view, the report and the summary print (I-7: each is derived
    from this, not re-read). `open_items`: the one open-item rule (`engine.open_review_state`);
    `audit`: the run's entries newest first, loaded only for the report (`with_report`)."""

    run: Run
    rows: list[CanonicalRow]
    groups: list[Group]
    flags: list[ControlFlag]
    items: list[ReviewItem]
    proof: Proof | None
    open_items: list[ReviewItem]
    norm_log: list[NormLogEntry] = field(default_factory=list)
    files: list[SourceFile] = field(default_factory=list)
    audit: list[AuditEntry] = field(default_factory=list)

    @property
    def current_keys(self) -> frozenset[str]:
        """For `report.exception_list` / `build_report_context`."""
        return frozenset(i.item_key for i in self.open_items)

    def exceptions(self) -> list[ExceptionItem]:
        return report.exception_list(
            self.run,
            self.groups,
            self.rows,
            self.flags,
            self.items,
            self.proof,
            current_keys=self.current_keys,
        )

    def report_context(self, run: Run | None = None) -> report.ReportContext:
        """The report of `run` (default: the run as read; pass it with the finalise instant
        and actor set, research R8)."""
        return report.build_report_context(
            run or self.run,
            self.groups,
            self.rows,
            self.flags,
            self.items,
            self.proof,
            self.norm_log,
            self.files,
            self.audit,
            current_keys=self.current_keys,
        )


def load_run_data(repo: Repo, run: Run, *, with_report: bool = False) -> RunData:
    """`RunData` of `run`: one read per table (the review items once, the audit once). The
    review items and the audit are read before the rows, groups and proof, so a reconcile
    their `unreconciled_decisions` check finds complete had written those first (IMP-1)."""
    items = repo.list_review_items(run.id)
    audit_log = repo.list_audit(run_id=run.id) if with_report else None
    open_items, _ = open_review_state(repo, run.id, items, audit_log)
    extra: dict[str, Any] = {}
    if with_report:
        extra = {
            "norm_log": repo.list_norm_log(run.id),
            "files": repo.list_source_files(run.id),
            "audit": audit_log,
        }
    return RunData(
        run=run,
        rows=repo.list_source_rows(run.id),
        groups=repo.list_groups(run.id),
        flags=repo.list_flags(run.id),
        items=items,
        proof=repo.get_proof(run.id),
        open_items=open_items,
        **extra,
    )


def load_run_view(repo: Repo, run_id: UUID) -> RunView:
    """The flat `RunView` of a run (NotFound for an unknown run). `open_review_items` follows
    the one open-item rule (`engine.open_review_state`, the same list the exceptions use);
    `summary` is the `SUMMARY:{run_key}` notification of this run (C7: never another run's
    with the same key), None before finalise."""
    return run_view(repo, load_run_data(repo, repo.get_run(run_id)))


def run_view(repo: Repo, data: RunData) -> RunView:
    """The `RunView` of `data` (reads only the stage progress and the summary row)."""
    run = data.run
    counts = outcome_counts_of(data.rows, data.groups)
    progress, read_cost = progress_and_cost(repo, run)
    return RunView(
        **run.model_dump(),
        stage_progress=progress,
        counts=counts.flat(),
        row_counts={s: {o: counts.rows_by_outcome[o][s] for o in Outcome} for s in Source},
        proof_status=None if data.proof is None else data.proof.status,
        open_review_items=len(data.open_items),
        headline=report.headline(data.groups),
        exceptions=data.exceptions(),
        summary=telegram.summary_of(repo, run),
        read_cost=read_cost,
    )


# --- frozen report files (F-09.R3 to R5; contracts/frozen-files.md, research R8, R9) ------


def report_paths(run_id: UUID) -> tuple[str, str]:
    """`reports/{run_id}/report.html` and `reports/{run_id}/outcomes.csv` (Section 10)."""
    return f"{run_id}/report.html", f"{run_id}/outcomes.csv"


def write_report_files(
    repo: Repo,
    store: BlobStore,
    run_id: UUID,
    finalise_at: datetime,
    actor: str,
    *,
    adopt: bool = False,
    data: RunData | None = None,
) -> tuple[str, str]:
    """Write `outcomes.csv` then `report.html`, each once (F-09.R5, research R9): an existing
    file is kept, never rewritten. The report prints `finalise_at` and `actor` (the email the
    run is finalised by), so the caller passes the same instant to `transition_run` (R8); it
    is written last, so a crash after the CSV leaves it missing and the retry prints the
    instant it actually finalises with. StateConflict (the caller must not finalise) when
    `report.html` already exists for a run that is not `FINALISED`, unless the caller `adopt`s
    it (its meta tags matched, `_freeze`), or when its write loses a race: the printed instant
    is then the other caller's. `data`: the run as `_freeze` read it (no second read)."""
    html_path, csv_path = report_paths(run_id)
    run = repo.get_run(run_id) if data is None else data.run
    html_exists = store.exists(REPORTS_BUCKET, html_path)
    if html_exists and run.status is not RunStatus.FINALISED and not adopt:
        raise StateConflict(run)
    csv_exists = store.exists(REPORTS_BUCKET, csv_path)
    if html_exists and csv_exists:
        return html_path, csv_path
    if data is None:
        data = load_run_data(repo, run, with_report=True)
    if not csv_exists:
        csv_bytes = report.render_outcomes_csv(data.rows, data.groups)
        _put_once(store, csv_path, csv_bytes, "text/csv; charset=utf-8")
    if not html_exists:
        frozen = run.model_copy(update={"finalised_at": finalise_at, "finalised_by": actor})
        html = report.render_report_html(data.report_context(frozen))
        if not _put_once(store, html_path, html, "text/html; charset=utf-8"):
            raise StateConflict(repo.get_run(run_id))
    return html_path, csv_path


def _put_once(store: BlobStore, path: str, data: bytes, content_type: str) -> bool:
    """True if this call wrote `path`; False if it already existed (written meanwhile)."""
    try:
        store.put(REPORTS_BUCKET, path, data, content_type)
    except UniqueViolation:
        return False
    return True


def report_links(repo: Repo, store: BlobStore, run_id: UUID) -> ReportLinks:
    """EP-18 (FR-024): signed links to both files, `DOWNLOAD_URL_S` (300 s); `StateConflict`
    (409 `RUN_STATE_CONFLICT`) unless the run is `FINALISED`; a file missing from storage is
    `NotFound` (404) where the store checks the object when signing (Supabase)."""
    run = repo.get_run(run_id)
    if run.status is not RunStatus.FINALISED:
        raise StateConflict(run)
    html_path, csv_path = report_paths(run_id)
    html_url, expires_at = store.signed_download_url(REPORTS_BUCKET, html_path)
    csv_url, _ = store.signed_download_url(REPORTS_BUCKET, csv_path)
    return ReportLinks(html_url=html_url, csv_url=csv_url, expires_at=expires_at)


# --- finalise (EP-17; F-10, plan key point 2, data-model.md §6 to §8) ----------------------

FINALISABLE = frozenset({RunStatus.AWAITING_REVIEW, RunStatus.READY})


def finalise(
    repo: Repo,
    store: BlobStore,
    notifier: Notifier,
    run_id: UUID,
    request: FinaliseRequest,
    actor: AllowedUser,
    channel: AuditChannel,
    *,
    settings: Settings | None = None,
    now: Callable[[], datetime] = clock.now,
) -> FinaliseResponse:
    """EP-17 (F-10.R1 to R7, FR-025 to FR-034). In order: `confirm` (ConfirmRequired), role
    `CONTROLLER` (403), the stale-summary check, proof `PASS` (409 `PROOF_NOT_PASSED`), status
    `AWAITING_REVIEW`/`READY` (freeze: one instant, report files, `FINALISED`, `RUN_FINALISED`)
    or `FINALISED` (a resend), else 409. Then the `SUMMARY:{run_key}` row by state: `SENT` →
    nothing sent; `SENDING` → 409 (stale: "check Telegram by hand"); `PENDING`/`FAILED` → one
    send pass. A Telegram failure is the 200 body's `FAILED` notification, never an error. C2:
    the summary text is built before the run freezes (a failure there is FM-10); on a resend
    it is rebuilt, and a failure there is a `FAILED` notification too."""
    settings = settings or get_settings()
    if request.confirm is not True:
        raise ConfirmRequired()
    if actor.role is not Role.CONTROLLER:
        raise ApiError(ErrorCode.NOT_ALLOWED, 403, "Your role does not allow this")
    stale = telegram.check_stale_summary(repo, notifier, run_id, settings=settings, now=now)
    proof = repo.get_proof(run_id)
    if proof is None or proof.status is not ProofStatus.PASS:
        raise ApiError(
            ErrorCode.PROOF_NOT_PASSED, 409, "The completeness proof has not passed", Stage.PROOF
        )
    run = repo.get_run(run_id)
    text: str | None = None
    data: RunData | None = None
    if run.status in FINALISABLE:
        run, text, data = _freeze(repo, store, notifier, run, actor, channel, proof, settings, now)
    elif run.status is RunStatus.FINALISED:
        _backfill_finalised_audit(repo, run, channel, now)
    else:
        raise StateConflict(run)
    n = _summary_row(repo, run, now)
    if n.status is NotificationStatus.SENDING:
        if stale is not None or telegram.is_stale(n, now()):
            message = "The summary send stalled; check Telegram by hand before anything else"
        else:
            message = "The summary send is in progress"
        details = {"status": run.status.value, "summary_status": n.status.value}
        raise ApiError(ErrorCode.RUN_STATE_CONFLICT, 409, message, Stage.NOTIFY, details)
    html_path, _ = report_paths(run.id)
    if n.status is not NotificationStatus.SENT and n.telegram_message_id is not None:
        text = ""  # M-5: the message was delivered; the resend sends report.html only
    if n.status is not NotificationStatus.SENT and text is None:
        try:
            data = load_run_data(repo, run)
            text = summary_text(data, proof, settings)
        except Exception as exc:
            n = telegram.summary_not_built(repo, notifier, run, n, exc, settings=settings, now=now)
    if n.status is not NotificationStatus.SENT and text is not None:
        html = store.get(REPORTS_BUCKET, html_path)
        try:
            n = telegram.send_summary(
                repo, notifier, run, n, text, html, settings=settings, now=now
            )
        except LedgerlineError:
            raise
        except Exception as exc:  # e.g. the repo after a send: the row stays SENDING (R12)
            raise ApiError(ErrorCode.INTERNAL_ERROR, 500, "Unexpected error", Stage.NOTIFY) from exc
    report_url, _ = store.signed_download_url(REPORTS_BUCKET, html_path)
    view = load_run_view(repo, run.id) if data is None else run_view(repo, replace(data, run=run))
    return FinaliseResponse(run=view, report_url=report_url, notification=n)


@dataclass(frozen=True)
class _SummarySource:
    """`telegram.SummarySource` from `RunData` (no `RunView` needed, I-7)."""

    id: UUID
    month: str
    counts: dict[Outcome, int]
    exceptions: list[ExceptionItem]


def summary_text(
    data: RunData,
    proof: Proof,
    settings: Settings,
    exceptions: list[ExceptionItem] | None = None,
) -> str:
    """The rendered summary message of the run as read (contracts/telegram-message.md);
    `exceptions`: `data.exceptions()` when the caller already has them."""
    source = _SummarySource(
        id=data.run.id,
        month=data.run.month,
        counts=outcome_counts_of(data.rows, data.groups).flat(),
        exceptions=data.exceptions() if exceptions is None else exceptions,
    )
    return telegram.build_summary_message(source, settings.APP_BASE_URL, proof).render()


_STAMPED = frozenset({AuditAction.REVIEW_RESOLVED, AuditAction.RECONCILED})


def _decision_stamp(items: list[ReviewItem], audit: list[AuditEntry]) -> tuple[object, ...]:
    """Cheap stand-in for the report fingerprint (I-7a): any review decision or re-reconcile
    changes the review items or adds a `REVIEW_RESOLVED` / `RECONCILED` audit entry (I2)."""
    stamped = sorted(a.id for a in audit if a.action in _STAMPED)
    return tuple(i.model_dump_json() for i in items), tuple(stamped)


STILL_APPLYING = "A review decision is still being applied; try again"
CHANGED_MIDWAY = (
    "The run changed: a review decision landed while the report was being written. Finalise "
    "again once it is applied; if the written report is out of date, ops is alerted to remove it"
)


def _still_applying() -> ApiError:
    return ApiError(ErrorCode.RUN_STATE_CONFLICT, 409, STILL_APPLYING, Stage.FINALISE)


def _changed_midway() -> ApiError:
    return ApiError(ErrorCode.RUN_STATE_CONFLICT, 409, CHANGED_MIDWAY, Stage.FINALISE)


def _recheck(repo: Repo, run: Run, data: RunData) -> bool:
    """Re-read the decisions (IMP-1): `_still_applying` if one is not reconciled yet; True if
    the run is still as `data` read it (the decision stamp)."""
    items, audit_log = repo.list_review_items(run.id), repo.list_audit(run_id=run.id)
    if unreconciled_decisions(items, audit_log):
        raise _still_applying()
    return _decision_stamp(items, audit_log) == _decision_stamp(data.items, data.audit)


def _freeze(
    repo: Repo,
    store: BlobStore,
    notifier: Notifier,
    run: Run,
    actor: AllowedUser,
    channel: AuditChannel,
    proof: Proof,
    settings: Settings,
    now: Callable[[], datetime],
) -> tuple[Run, str, RunData]:
    """F-10.R2, R3: one instant for the report and the run; the summary text (C2), then the
    report files, then `FINALISED` from the status read at the start (I2). A text or report
    failure (FM-10) leaves the run unchanged, writes `finalise FAILED` and alerts ops; so does
    a failed transition after the report was written. A StateConflict (a concurrent finalise
    or decision) is only the 409. An existing report.html (an earlier finalise that never
    transitioned, research R9) is adopted with its own instant and actor when its fingerprint
    is the run's; otherwise 409 and one ops alert, as for a stored outcomes.csv of other data
    (I-1). The run is read once (`load_run_data`, I-7); the text, fingerprint, CSV and HTML
    come from that read, and a cheap decision stamp re-checks it before `FINALISED`. Returns
    the frozen run, the text and the read; an adopted report is audited `adopted: true`.
    IMP-1, IMP-2 (research R9): a decided review item that no reconcile has applied yet
    (`unreconciled_decisions`) is 409 "still being applied", checked on the read, again just
    before the first file write and again before `FINALISED` (and by migration 0003 in the
    database at `FINALISED`). A decision landing after that last pre-write check is the 409
    `CHANGED_MIDWAY`; the write-once files it leaves are adopted if they still match, else
    the orphan route."""
    at, by = now(), actor.email
    adopted = False
    try:
        with stage_step(repo, run.id, Stage.FINALISE, now=now):
            try:
                data = load_run_data(repo, run, with_report=True)
                if unreconciled_decisions(data.items, data.audit):
                    raise _still_applying()
                ctx = data.report_context()
                fingerprint = report.report_fingerprint(ctx)  # M-1: before the text
                text = summary_text(data, proof, settings, ctx.exceptions)
                orphan = _orphan_report(store, run.id)
                csv_bytes = report.render_outcomes_csv(data.rows, data.groups)
                stored_csv = _stored(store, report_paths(run.id)[1])
                if (stored_csv is not None and stored_csv != csv_bytes) or (
                    orphan is not None
                    and (orphan.meta is None or orphan.meta.fingerprint != fingerprint)
                ):
                    _orphan_mismatch(repo, notifier, run, settings, now)
                if orphan is not None and orphan.meta is not None:
                    at, by, adopted = orphan.meta.finalised_at, orphan.meta.finalised_by, True
                if not _recheck(repo, run, data):  # last check before the write-once files
                    raise StateConflict(repo.get_run(run.id))
                paths = write_report_files(repo, store, run.id, at, by, adopt=adopted, data=data)
            except (StateConflict, ApiError):
                raise
            except Exception as exc:
                stage_failed_alert(
                    repo, notifier, run, Stage.FINALISE, exc, settings=settings, now=now
                )
                raise
            try:
                unchanged = _recheck(repo, run, data)
            except ApiError as exc:
                raise _changed_midway() from exc
            if not unchanged:  # a decision meanwhile (I2)
                raise _changed_midway()
            try:
                frozen = repo.transition_run(
                    run.id,
                    RunStatus.FINALISED,
                    expected={run.status},
                    finalised_by=by,
                    finalised_at=at,
                )
            except StateConflict as exc:  # a decision or reconcile since (migration 0003)
                if isinstance(exc.current, Run) and exc.current.status is RunStatus.FINALISED:
                    raise
                raise _changed_midway() from exc
            except UniqueViolation:
                raise
            except Exception as exc:  # report.html is written: the next call adopts it
                stage_failed_alert(
                    repo, notifier, run, Stage.FINALISE, exc, settings=settings, now=now
                )
                raise
            audit(
                repo,
                actor.email,
                AuditAction.RUN_FINALISED,
                "run",
                str(run.id),
                run_id=run.id,
                before={"status": run.status.value, "open_review_items": len(data.open_items)},
                after={
                    "status": frozen.status.value,
                    "finalised_by": by,
                    "finalised_at": at.isoformat(),
                    "report_paths": list(paths),
                    **({"adopted": True} if adopted else {}),
                },
                channel=channel,
                now=lambda: at,
            )
    except (StateConflict, UpstreamFailed, ApiError):
        raise
    except UniqueViolation as exc:  # C7: these inputs were finalised by another run meanwhile
        other = repo.find_finalised_run_by_key(run.run_key or "")
        details = {"run_id": str(other.id)} if other is not None else {}
        message = "This exact month and files were already finalised"
        raise ApiError(ErrorCode.DUPLICATE_RUN, 409, message, Stage.FINALISE, details) from exc
    except Exception as exc:
        raise ApiError(ErrorCode.INTERNAL_ERROR, 500, "Unexpected error", Stage.FINALISE) from exc
    return frozen, text, data


def _stored(store: BlobStore, path: str) -> bytes | None:
    """The stored report file at `path`, None if there is none."""
    return store.get(REPORTS_BUCKET, path) if store.exists(REPORTS_BUCKET, path) else None


ORPHAN_GROUP = "orphan-report"
"""`ALERT:{run_id}:finalise:orphan-report`: the one ops alert for a report.html left by an
earlier finalise for different data (research R9)."""


@dataclass(frozen=True)
class _Orphan:
    meta: report.ReportMeta | None


def _orphan_report(store: BlobStore, run_id: UUID) -> _Orphan | None:
    """The report.html of a run that is not `FINALISED`, if one exists (research R9)."""
    html_path, _ = report_paths(run_id)
    if not store.exists(REPORTS_BUCKET, html_path):
        return None
    return _Orphan(meta=report.read_report_meta(store.get(REPORTS_BUCKET, html_path)))


def _orphan_mismatch(
    repo: Repo, notifier: Notifier, run: Run, settings: Settings, now: Callable[[], datetime]
) -> NoReturn:
    """The orphan report.html or the stored outcomes.csv prints other data (or has no meta
    tags): never frozen as this run's report. One ops alert ever; the controller gets the
    409."""
    error = ApiError(
        ErrorCode.RUN_STATE_CONFLICT,
        409,
        "An earlier finalise attempt wrote a report for data that has changed since (e.g. a "
        "review decision made during it); the report files are write-once, so ops must remove "
        "it before this run can be finalised: remove report.html and outcomes.csv",
        Stage.FINALISE,
    )
    stage_failed_alert(
        repo,
        notifier,
        run,
        Stage.FINALISE,
        error,
        settings=settings,
        now=now,
        attempt_group=ORPHAN_GROUP,
    )
    raise error


def _backfill_finalised_audit(
    repo: Repo, run: Run, channel: AuditChannel, now: Callable[[], datetime]
) -> None:
    """M4: a crash between `FINALISED` and its `RUN_FINALISED` audit is repaired by the next
    EP-17 call; the entry says it was back-filled (`before` unknown)."""
    if any(a.action is AuditAction.RUN_FINALISED for a in repo.list_audit(run_id=run.id)):
        return
    audit(
        repo,
        run.finalised_by or "system",
        AuditAction.RUN_FINALISED,
        "run",
        str(run.id),
        run_id=run.id,
        before=None,
        after={
            "status": run.status.value,
            "finalised_by": run.finalised_by,
            "finalised_at": None if run.finalised_at is None else run.finalised_at.isoformat(),
            "report_paths": list(report_paths(run.id)),
            "backfilled": True,
        },
        channel=channel,
        now=now,
    )


def _summary_row(repo: Repo, run: Run, now: Callable[[], datetime]) -> Notification:
    """The run's `SUMMARY:{run_key}` row, inserted `PENDING` if none (F-10.R4); also after a
    crash between `FINALISED` and the insert. Another run's row under the key is a conflict."""
    if run.run_key is None:
        raise StateConflict(run)
    n, _ = repo.insert_notification(
        Notification(
            id=uuid4(),
            run_id=run.id,
            kind=NotificationKind.SUMMARY,
            idempotency_key=summary_key(run.run_key),
            status=NotificationStatus.PENDING,
            attempts=0,
            last_error=None,
            telegram_message_id=None,
            sent_at=None,
            updated_at=now(),
        )
    )
    if n.run_id != run.id:
        raise StateConflict(run)
    return n
