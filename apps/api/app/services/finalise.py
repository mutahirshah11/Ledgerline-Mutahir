"""Run overview data and (later) finalise (005 plan "Complexity Tracking"; data-model.md §2,
research R15; F-09, F-10, SCR-04).

`load_run_view` is the EP-05 body and the `run` of EP-15 and EP-17: the Phase 3 `RunDetail`
(Run fields, stage progress, counts, proof status, open review items) plus row counts, the
headline, the exception list and the summary notification (C25, FR-020).
"""

from uuid import UUID

from app.core.config import get_settings
from app.core.db import Repo
from app.domain.enums import Outcome, ReadVariant, RunEventStatus, Source, Stage
from app.domain.ids import summary_key
from app.domain.models import (
    PageProgress,
    Run,
    RunEvent,
    RunView,
    StageProgress,
    StageProgressView,
    StageState,
)
from app.pipeline import report
from app.pipeline.engine import open_review_count, outcome_counts_of

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


def load_run_view(repo: Repo, run_id: UUID) -> RunView:
    """The flat `RunView` of a run (NotFound for an unknown run). `open_review_items` follows
    the one open-item rule (`engine.open_review_count`); `summary` is the `SUMMARY:{run_key}`
    notification, None before finalise."""
    run = repo.get_run(run_id)
    rows = repo.list_source_rows(run_id)
    groups = repo.list_groups(run_id)
    proof = repo.get_proof(run_id)
    counts = outcome_counts_of(rows, groups)
    exceptions = report.exception_list(
        run, groups, rows, repo.list_flags(run_id), repo.list_review_items(run_id), proof
    )
    summary = repo.get_notification_by_key(summary_key(run.run_key)) if run.run_key else None
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
