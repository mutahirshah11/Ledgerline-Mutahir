"""Runs and uploads: EP-03 to EP-07 (F-01; data-model.md §2, contracts/openapi.yaml, C25).

EP-03, EP-06 and EP-07 are CONTROLLER only (Clarification 4); EP-04 and EP-05 are open to every
allow-listed role. Upload logic lives in `app.services.stages`.
"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import require_role
from app.core.config import get_settings
from app.core.db import Repo, get_repo
from app.core.storage import BlobStore, get_blobstore
from app.domain.enums import (
    Outcome,
    ProofStatus,
    ReadVariant,
    ReviewStatus,
    Role,
    RunEventStatus,
    RunStatus,
    Source,
    Stage,
)
from app.domain.models import AllowedUser, Run, RunEvent
from app.services import stages
from app.services.stages import UploadFileIn, UploadSlot

router = APIRouter()

RepoDep = Annotated[Repo, Depends(get_repo)]
BlobsDep = Annotated[BlobStore, Depends(get_blobstore)]
Controller = Annotated[AllowedUser, Depends(require_role(Role.CONTROLLER))]


# --- models (data-model.md §2) ---------------------------------------------------------


class CreateRunRequest(BaseModel):
    month: str


class RunOut(BaseModel):
    id: UUID
    month: str
    status: RunStatus
    run_key: str | None
    failed_stage: Stage | None
    error_message: str | None
    scenario_now: datetime
    created_by: str
    created_at: datetime
    finalised_at: datetime | None
    finalised_by: str | None

    @classmethod
    def of(cls, run: Run) -> "RunOut":
        return cls.model_validate(run.model_dump())


class UploadRequest(BaseModel):
    files: list[UploadFileIn]


class UploadResponse(BaseModel):
    uploads: list[UploadSlot]


class ConfirmResponse(BaseModel):
    run: RunOut
    new_pages: list[int]
    known_pages: list[int]


StageState = Literal["PENDING", "RUNNING", "OK", "FAILED"]


class StageProgress(BaseModel):
    stage: Stage
    state: StageState
    at: datetime | None
    duration_ms: int | None


class PageProgress(BaseModel):
    page_no: int
    extracted: bool
    cached: bool


class StageProgressView(BaseModel):
    stages: list[StageProgress]
    pages: list[PageProgress]


class RunDetail(RunOut):
    """EP-05, flat (C25); Phase 4 adds headline, exceptions, summary and row_counts."""

    stage_progress: StageProgressView
    counts: dict[Outcome, int]
    proof_status: ProofStatus | None
    open_review_items: int


class RunSummary(BaseModel):
    id: UUID
    month: str
    status: RunStatus
    created_at: datetime
    counts: dict[Outcome, int]
    proof_status: ProofStatus | None


# --- views -----------------------------------------------------------------------------

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
    """data-model.md §2 `StageProgressView.pages`. `extracted`: both reads under the current model
    and prompt (§3). `cached`: both reads existed before this run's extract stage first touched
    the page, read from the run's extract OK event; without one, both reads are older than the
    run itself."""
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


def _latest(events: list[RunEvent], stage: Stage) -> RunEvent | None:
    """The newest event of `stage`; `list_run_events` is newest first, ties newest insertion."""
    return next((e for e in events if e.stage is stage), None)


def _stage_progress(repo: Repo, run: Run) -> StageProgressView:
    events = repo.list_run_events(run.id)
    pages = _pages(repo, run, events)
    out: list[StageProgress] = []
    for stage in PROGRESS_STAGES:
        last = _latest(events, stage)
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


def _proof_status(repo: Repo, run_id: UUID) -> ProofStatus | None:
    proof = repo.get_proof(run_id)
    return None if proof is None else proof.status


# --- endpoints -------------------------------------------------------------------------


@router.post("/runs", status_code=201, response_model=RunOut)
def create_run(body: CreateRunRequest, user: Controller, repo: RepoDep) -> RunOut:
    """EP-03 (F-01.R1)."""
    return RunOut.of(stages.create_run(repo, user, body.month))


@router.get("/runs", response_model=list[RunSummary])
def list_runs(repo: RepoDep, limit: Annotated[int, Query(ge=1, le=100)] = 20) -> list[RunSummary]:
    """EP-04: newest first, counts by outcome and proof status (FR-013)."""
    return [
        RunSummary(
            id=run.id,
            month=run.month,
            status=run.status,
            created_at=run.created_at,
            counts=stages.run_outcome_counts(repo, run.id),
            proof_status=_proof_status(repo, run.id),
        )
        for run in repo.list_runs(limit)
    ]


@router.get("/runs/{id}", response_model=RunDetail)
def get_run(id: UUID, repo: RepoDep) -> RunDetail:
    """EP-05: flat `RunDetail` (C25)."""
    run = repo.get_run(id)
    return RunDetail(
        **RunOut.of(run).model_dump(),
        stage_progress=_stage_progress(repo, run),
        counts=stages.run_outcome_counts(repo, run.id),
        proof_status=_proof_status(repo, run.id),
        open_review_items=len(repo.list_review_items(run.id, ReviewStatus.OPEN)),
    )


@router.post("/runs/{id}/uploads", response_model=UploadResponse)
def request_uploads(
    id: UUID, body: UploadRequest, user: Controller, repo: RepoDep, blobs: BlobsDep
) -> UploadResponse:
    """EP-06 (F-01.R2, R3)."""
    run = repo.get_run(id)
    return UploadResponse(uploads=stages.request_uploads(repo, blobs, run, body.files))


@router.post("/runs/{id}/uploads/confirm", response_model=ConfirmResponse)
def confirm_uploads(id: UUID, user: Controller, repo: RepoDep, blobs: BlobsDep) -> ConfirmResponse:
    """EP-07 (F-01.R4 to R7); idempotent."""
    result = stages.confirm_uploads(repo, blobs, repo.get_run(id), user)
    return ConfirmResponse(
        run=RunOut.of(result.run), new_pages=result.new_pages, known_pages=result.known_pages
    )
