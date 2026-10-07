"""Runs, uploads and stages: EP-03 to EP-10 (F-01 to F-07; data-model.md §2, contracts/openapi.yaml,
C25).

EP-03, EP-06 and EP-07 are CONTROLLER only (Clarification 4); EP-04 and EP-05 are open to every
allow-listed role. Upload logic lives in `app.services.stages`. Phase 4 (005): EP-05 returns
`RunView` (`app.services.finalise.load_run_view`) and checks a finalised run for a stale summary;
EP-11 to EP-13 read groups, flags and proof; EP-17 finalises (`app.services.finalise.finalise`);
EP-18 links the frozen report files.
"""

from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel

from app.core.auth import require_role
from app.core.config import get_settings
from app.core.db import Repo, get_repo
from app.core.deps import get_extract_sleep, get_model_client, get_notifier
from app.core.errors import NotFound
from app.core.faults import Fault, active_fault
from app.core.storage import BlobStore, get_blobstore
from app.domain.enums import (
    AuditAction,
    AuditChannel,
    NormCode,
    Outcome,
    ProofStatus,
    Role,
    RunEventStatus,
    RunStatus,
    Stage,
)
from app.domain.models import (
    AllowedUser,
    ControlFlag,
    DecimalStr,
    FinaliseRequest,
    FinaliseResponse,
    GroupView,
    Proof,
    ReportLinks,
    Run,
    RunView,
)
from app.pipeline import report
from app.pipeline.cost import estimate_read_cost
from app.pipeline.model_client import ModelClient
from app.pipeline.validate import ValidationProblem
from app.routes._common import READ_ERRORS, channel
from app.services import finalise, stages
from app.services.stages import ExtractPageResult, ReconcileResult, UploadFileIn, UploadSlot
from app.services.telegram import Notifier, TelegramNotifier, check_stale_summary

router = APIRouter()

RepoDep = Annotated[Repo, Depends(get_repo)]
BlobsDep = Annotated[BlobStore, Depends(get_blobstore)]
NotifierDep = Annotated[Notifier, Depends(get_notifier)]
Controller = Annotated[AllowedUser, Depends(require_role(Role.CONTROLLER))]
StageCaller = Annotated[AllowedUser, Depends(require_role(Role.CONTROLLER, ops_retry=True))]
ModelClientDep = Annotated[ModelClient, Depends(get_model_client)]
SleepDep = Annotated[Callable[[float], None], Depends(get_extract_sleep)]
FaultDep = Annotated[Fault | None, Depends(active_fault)]


def get_summary_notifier(notifier: NotifierDep, fault: FaultDep) -> Iterator[Notifier]:
    """EP-17's notifier: under fault `TELEGRAM_DOWN` (Section 25) a real notifier that fails
    every attempt without a network call, so its retries and run events are the real ones."""
    if fault is not Fault.TELEGRAM_DOWN:
        yield notifier
        return
    with TelegramNotifier(get_settings(), fault=fault) as down:
        yield down


SummaryNotifierDep = Annotated[Notifier, Depends(get_summary_notifier)]


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
    """EP-07. `read_cost_estimate_usd` (006 read cost, `app.pipeline.cost`): `new_pages` x the
    per-page token averages x the price settings, US$ to 0.001; None while a price is unset.
    Known pages come from the extraction cache at US$0."""

    run: RunOut
    new_pages: list[int]
    known_pages: list[int]
    read_cost_estimate_usd: DecimalStr | None


class ValidateResponse(BaseModel):
    ok: Literal[True]
    problems: list[ValidationProblem]


class RunSummary(BaseModel):
    id: UUID
    month: str
    status: RunStatus
    created_at: datetime
    counts: dict[Outcome, int]
    proof_status: ProofStatus | None


class RunEventOut(BaseModel):
    """EP-19 `RunEvent` (data-model.md §2); newest first (FR-034)."""

    id: UUID
    stage: Stage
    status: RunEventStatus
    at: datetime
    duration_ms: int | None
    detail: dict[str, Any]


class AuditEntryOut(BaseModel):
    """EP-20 `AuditEntry` (data-model.md §2); newest first, optional `?actor=` (FR-034)."""

    id: int
    at: datetime
    actor: str
    action: AuditAction
    entity: str
    entity_id: str
    run_id: UUID | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    channel: AuditChannel


class NormLogEntryOut(BaseModel):
    """EP-21 `NormLogEntry` (data-model.md §2), filtered by `?source_row_id=` (FR-034)."""

    source_row_id: str
    field: str
    code: NormCode
    before: str | None
    after: str | None


# --- views -----------------------------------------------------------------------------


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


@router.get("/runs/{id}", response_model=RunView)
def get_run(id: UUID, repo: RepoDep, notifier: NotifierDep) -> RunView:
    """EP-05: flat `RunView` (C25, FR-020): Phase 3 `RunDetail` plus row counts, headline,
    exception list and summary notification (005 data-model.md §2, E3). A `FINALISED` run is
    checked for a stale summary first (F-10.R7, C22: one ops alert, never a resend)."""
    if repo.get_run(id).status is RunStatus.FINALISED:
        check_stale_summary(repo, notifier, id)
    return finalise.load_run_view(repo, id)


@router.post("/runs/{id}/uploads", response_model=UploadResponse)
def request_uploads(
    id: UUID, body: UploadRequest, user: Controller, repo: RepoDep, blobs: BlobsDep
) -> UploadResponse:
    """EP-06 (F-01.R2, R3)."""
    run = repo.get_run(id)
    return UploadResponse(uploads=stages.request_uploads(repo, blobs, run, body.files))


@router.post("/runs/{id}/uploads/confirm", response_model=ConfirmResponse)
def confirm_uploads(
    id: UUID, user: Controller, repo: RepoDep, blobs: BlobsDep, notifier: NotifierDep
) -> ConfirmResponse:
    """EP-07 (F-01.R4 to R7); idempotent."""
    result = stages.confirm_uploads(repo, blobs, repo.get_run(id), user, notifier=notifier)
    return ConfirmResponse(
        run=RunOut.of(result.run),
        new_pages=result.new_pages,
        known_pages=result.known_pages,
        read_cost_estimate_usd=estimate_read_cost(len(result.new_pages), get_settings()),
    )


@router.post("/runs/{id}/validate", response_model=ValidateResponse)
def validate_run(
    id: UUID, user: StageCaller, repo: RepoDep, blobs: BlobsDep, notifier: NotifierDep
) -> ValidateResponse:
    """EP-08 (F-02.R5); CONTROLLER always, OPS only to retry a `FAILED` run (contracts/auth.md);
    validate itself has no retry from `FAILED` (data-model.md §3, FR-024)."""
    problems = stages.validate_stage(repo, blobs, notifier, repo.get_run(id), user)
    return ValidateResponse(ok=True, problems=problems)


@router.post("/runs/{id}/pages/{page_no}/extract", response_model=ExtractPageResult)
def extract_page(
    id: UUID,
    page_no: Annotated[int, Path(ge=1, le=10)],
    user: StageCaller,
    repo: RepoDep,
    blobs: BlobsDep,
    notifier: NotifierDep,
    client: ModelClientDep,
    sleep: SleepDep,
    fault: FaultDep,
) -> ExtractPageResult:
    """EP-09 (F-03, FR-019, FR-020); CONTROLLER, or OPS to retry a `FAILED` run; idempotent."""
    run = repo.get_run(id)
    return stages.extract_stage(
        repo, blobs, notifier, run, page_no, user, fault, client=client, sleep=sleep
    )


@router.post("/runs/{id}/reconcile", response_model=ReconcileResult)
def reconcile(
    id: UUID,
    user: StageCaller,
    repo: RepoDep,
    blobs: BlobsDep,
    notifier: NotifierDep,
    fault: FaultDep,
) -> ReconcileResult:
    """EP-10 (F-04 to F-07, FR-021, FR-022, C25); CONTROLLER, or OPS to retry a `FAILED` run."""
    return stages.reconcile_stage(repo, blobs, notifier, repo.get_run(id), user, fault)


# --- read endpoints (US6, FR-034) -------------------------------------------------------
# EP-19 to EP-21 are open to every allow-listed role (contracts/auth.md); the protected
# router's `current_user` dependency is the only guard. `repo.get_run` 404s an unknown run.


@router.get("/runs/{id}/events", response_model=list[RunEventOut])
def list_run_events(id: UUID, repo: RepoDep) -> list[RunEventOut]:
    """EP-19: the run's events, newest first, with durations (F-11.R2, FR-034)."""
    repo.get_run(id)
    return [RunEventOut.model_validate(e.model_dump()) for e in repo.list_run_events(id)]


@router.get("/runs/{id}/audit", response_model=list[AuditEntryOut])
def list_run_audit(
    id: UUID, repo: RepoDep, actor: Annotated[str | None, Query()] = None
) -> list[AuditEntryOut]:
    """EP-20: the run's audit entries, newest first, optionally filtered by `actor` (FR-034)."""
    repo.get_run(id)
    return [
        AuditEntryOut.model_validate(a.model_dump())
        for a in repo.list_audit(run_id=id, actor=actor)
    ]


@router.get("/runs/{id}/normalisation-log", response_model=list[NormLogEntryOut])
def list_norm_log(
    id: UUID, repo: RepoDep, source_row_id: Annotated[str | None, Query()] = None
) -> list[NormLogEntryOut]:
    """EP-21: the run's normalisation log, optionally filtered by `source_row_id` (F-04, FR-034)."""
    repo.get_run(id)
    return [
        NormLogEntryOut.model_validate(n.model_dump())
        for n in repo.list_norm_log(id, source_row_id)
    ]


# --- results (005 US2: EP-11 to EP-13, FR-021) -------------------------------------------
# Open to every allow-listed role (contracts/auth.md); `repo.get_run` 404s an unknown run.


@router.get("/runs/{id}/groups", response_model=list[GroupView], responses=READ_ERRORS)
def list_groups(
    id: UUID, repo: RepoDep, outcome: Annotated[Outcome | None, Query()] = None
) -> list[GroupView]:
    """EP-11: groups in `list_groups` order, optionally by outcome, each with every source
    row's canonical and raw values and flags; diffs and outlier on the group (F-05, F-09)."""
    repo.get_run(id)
    groups = repo.list_groups(id, outcome)
    return report.group_views(groups, repo.list_source_rows(id), repo.list_flags(id))


@router.get("/runs/{id}/flags", response_model=list[ControlFlag], responses=READ_ERRORS)
def list_flags(id: UUID, repo: RepoDep) -> list[ControlFlag]:
    """EP-12: the control flags of the latest reconcile (F-06)."""
    repo.get_run(id)
    return repo.list_flags(id)


@router.get("/runs/{id}/proof", response_model=Proof, responses=READ_ERRORS)
def get_proof(id: UUID, repo: RepoDep) -> Proof:
    """EP-13: the completeness proof, Section 19 shape (F-07); 404 before the first reconcile."""
    repo.get_run(id)
    proof = repo.get_proof(id)
    if proof is None:
        raise NotFound("proof", id)
    return proof


# --- delivery (005 US3: EP-18, FR-024) -----------------------------------------------------


@router.get(
    "/runs/{id}/report",
    response_model=ReportLinks,
    responses={**READ_ERRORS, 409: {"description": "RUN_STATE_CONFLICT"}},
)
def report_links(id: UUID, repo: RepoDep, blobs: BlobsDep) -> ReportLinks:
    """EP-18: signed links to `report.html` and `outcomes.csv`, valid 5 minutes (F-09.R5);
    409 `RUN_STATE_CONFLICT` unless the run is `FINALISED`; open to every allow-listed role."""
    return finalise.report_links(repo, blobs, id)


@router.post(
    "/runs/{id}/finalise",
    response_model=FinaliseResponse,
    responses={
        **READ_ERRORS,
        400: {"description": "CONFIRM_REQUIRED"},
        409: {"description": "PROOF_NOT_PASSED or RUN_STATE_CONFLICT"},
        502: {"description": "UpstreamFailed"},
    },
)
def finalise_run(
    id: UUID,
    request: Request,
    user: Controller,
    repo: RepoDep,
    blobs: BlobsDep,
    notifier: SummaryNotifierDep,
    body: FinaliseRequest | None = None,
) -> FinaliseResponse:
    """EP-17 (F-10, F-07 AC6, F-12 AC3): CONTROLLER only; 400 `CONFIRM_REQUIRED` without
    `{"confirm": true}`; 409 `PROOF_NOT_PASSED` or `RUN_STATE_CONFLICT`; 200 with the summary
    notification (`SENT`, or `FAILED` when Telegram is down; call again to resend)."""
    return finalise.finalise(
        repo, blobs, notifier, id, body or FinaliseRequest(), user, channel(request)
    )
