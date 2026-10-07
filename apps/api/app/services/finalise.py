"""Run overview data and finalise (005 plan "Complexity Tracking", key point 2; data-model.md
§2, §6 to §8, research R8, R9, R15; F-09, F-10, SCR-04).

`load_run_view` is the EP-05 body and the `run` of EP-15 and EP-17: the Phase 3 `RunDetail`
(Run fields, stage progress, counts, proof status, open review items) plus row counts, the
headline, the exception list and the summary notification (C25, FR-020). `finalise` is EP-17.
"""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID, uuid4

from app.core import clock
from app.core.config import Settings, get_settings
from app.core.db import Repo
from app.core.errors import (
    ApiError,
    ConfirmRequired,
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
from app.domain.models import (
    AllowedUser,
    FinaliseRequest,
    FinaliseResponse,
    Notification,
    PageProgress,
    ReportLinks,
    Run,
    RunEvent,
    RunView,
    StageProgress,
    StageProgressView,
    StageState,
)
from app.pipeline import report
from app.pipeline.engine import open_review_count, open_review_items, outcome_counts_of
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


def _pages(repo: Repo, run: Run, events: list[RunEvent]) -> list[PageProgress]:
    """`StageProgressView.pages`. `extracted`: both reads under the current model and prompt.
    `cached`: both reads existed before this run's extract stage first touched the page, read
    from the run's extract OK event; without one, both reads are older than the run itself."""
    settings = get_settings()
    from_events = _extract_cached(events)
    out: list[PageProgress] = []
    files = repo.list_source_files(run.id, Source.REGISTER)
    for f in sorted(files, key=lambda f: f.page_no or 0):
        page_no = f.page_no or 0
        reads = [
            repo.get_extraction(f.sha256, v, settings.EXTRACT_MODEL, settings.PROMPT_VERSION)
            for v in ReadVariant
        ]
        extracted = all(r is not None for r in reads)
        if page_no in from_events:
            cached = extracted and from_events[page_no]
        else:
            cached = extracted and all(
                r is not None and r.created_at < run.created_at for r in reads
            )
        out.append(PageProgress(page_no=page_no, extracted=extracted, cached=cached))
    return out


def stage_progress(repo: Repo, run: Run) -> StageProgressView:
    """Per stage: the state of its newest event (`list_run_events` is newest first); extract
    stays `RUNNING` until every page is read."""
    events = repo.list_run_events(run.id)
    pages = _pages(repo, run, events)
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
    return StageProgressView(stages=out, pages=pages)


# --- run view (FR-020) ---------------------------------------------------------------------


def current_item_keys(repo: Repo, run_id: UUID) -> frozenset[str]:
    """The keys of the current OPEN items (the one open-item rule, `engine.open_review_items`),
    for `report.exception_list` / `build_report_context`."""
    return frozenset(i.item_key for i in open_review_items(repo, run_id))


def load_run_view(repo: Repo, run_id: UUID) -> RunView:
    """The flat `RunView` of a run (NotFound for an unknown run). `open_review_items` follows
    the one open-item rule (`engine.open_review_count`); `summary` is the `SUMMARY:{run_key}`
    notification of this run (C7: never another run's with the same key), None before
    finalise."""
    run = repo.get_run(run_id)
    rows = repo.list_source_rows(run_id)
    groups = repo.list_groups(run_id)
    proof = repo.get_proof(run_id)
    counts = outcome_counts_of(rows, groups)
    exceptions = report.exception_list(
        run,
        groups,
        rows,
        repo.list_flags(run_id),
        repo.list_review_items(run_id),
        proof,
        current_keys=current_item_keys(repo, run_id),
    )
    summary = telegram.summary_of(repo, run)
    return RunView(
        **run.model_dump(),
        stage_progress=stage_progress(repo, run),
        counts=counts.flat(),
        row_counts={s: {o: counts.rows_by_outcome[o][s] for o in Outcome} for s in Source},
        proof_status=None if proof is None else proof.status,
        open_review_items=open_review_count(repo, run_id),
        headline=report.headline(groups),
        exceptions=exceptions,
        summary=summary,
    )


# --- frozen report files (F-09.R3 to R5; contracts/frozen-files.md, research R8, R9) ------


def report_paths(run_id: UUID) -> tuple[str, str]:
    """`reports/{run_id}/report.html` and `reports/{run_id}/outcomes.csv` (Section 10)."""
    return f"{run_id}/report.html", f"{run_id}/outcomes.csv"


def write_report_files(
    repo: Repo, store: BlobStore, run_id: UUID, finalise_at: datetime, actor: str
) -> tuple[str, str]:
    """Write `outcomes.csv` then `report.html`, each once (F-09.R5, research R9): an existing
    file is kept, never rewritten. The report prints `finalise_at` and `actor` (the email the
    run is finalised by), so the caller passes the same instant to `transition_run` (R8); it
    is written last, so a crash after the CSV leaves it missing and the retry prints the
    instant it actually finalises with. StateConflict (the caller must not finalise) when
    `report.html` already exists for a run that is not `FINALISED` (another finalise is under
    way, or one crashed before its transition), or when its write loses a race: the printed
    instant is then the other caller's."""
    html_path, csv_path = report_paths(run_id)
    run = repo.get_run(run_id)
    html_exists = store.exists(REPORTS_BUCKET, html_path)
    if html_exists and run.status is not RunStatus.FINALISED:
        raise StateConflict(run)
    csv_exists = store.exists(REPORTS_BUCKET, csv_path)
    if html_exists and csv_exists:
        return html_path, csv_path
    rows = repo.list_source_rows(run_id)
    groups = repo.list_groups(run_id)
    if not csv_exists:
        csv_bytes = report.render_outcomes_csv(rows, groups)
        _put_once(store, csv_path, csv_bytes, "text/csv; charset=utf-8")
    if not html_exists:
        frozen = run.model_copy(update={"finalised_at": finalise_at, "finalised_by": actor})
        ctx = report.build_report_context(
            frozen,
            groups,
            rows,
            repo.list_flags(run_id),
            repo.list_review_items(run_id),
            repo.get_proof(run_id),
            repo.list_norm_log(run_id),
            repo.list_source_files(run_id),
            repo.list_audit(run_id=run_id),
            current_keys=current_item_keys(repo, run_id),
        )
        html = report.render_report_html(ctx)
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
    send pass. A Telegram failure is the 200 body's `FAILED` notification, never an error."""
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
    if run.status in FINALISABLE:
        run = _freeze(repo, store, notifier, run, actor, channel, settings, now)
    elif run.status is not RunStatus.FINALISED:
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
    if n.status is not NotificationStatus.SENT:
        view = load_run_view(repo, run.id)
        text = telegram.build_summary_message(view, settings.APP_BASE_URL, proof).render()
        html = store.get(REPORTS_BUCKET, html_path)
        n = telegram.send_summary(repo, notifier, run, n, text, html, settings=settings, now=now)
    report_url, _ = store.signed_download_url(REPORTS_BUCKET, html_path)
    return FinaliseResponse(run=load_run_view(repo, run.id), report_url=report_url, notification=n)


def _freeze(
    repo: Repo,
    store: BlobStore,
    notifier: Notifier,
    run: Run,
    actor: AllowedUser,
    channel: AuditChannel,
    settings: Settings,
    now: Callable[[], datetime],
) -> Run:
    """F-10.R2, R3: one instant for the report and the run; report files first. A report
    failure (FM-10) leaves the run unchanged, writes `finalise FAILED` and alerts ops; a
    StateConflict (a concurrent finalise) is only the 409."""
    at = now()
    open_items = open_review_count(repo, run.id)
    try:
        with stage_step(repo, run.id, Stage.FINALISE, now=now):
            try:
                paths = write_report_files(repo, store, run.id, at, actor.email)
            except StateConflict:
                raise
            except Exception as exc:
                stage_failed_alert(
                    repo, notifier, run, Stage.FINALISE, exc, settings=settings, now=now
                )
                raise
            frozen = repo.transition_run(
                run.id,
                RunStatus.FINALISED,
                expected=set(FINALISABLE),
                finalised_by=actor.email,
                finalised_at=at,
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
    audit(
        repo,
        actor.email,
        AuditAction.RUN_FINALISED,
        "run",
        str(run.id),
        run_id=run.id,
        before={"status": run.status.value, "open_review_items": open_items},
        after={
            "status": frozen.status.value,
            "finalised_by": actor.email,
            "finalised_at": at.isoformat(),
            "report_paths": list(paths),
        },
        channel=channel,
        now=lambda: at,
    )
    return frozen


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
