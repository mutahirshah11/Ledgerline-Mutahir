"""Review: EP-14 to EP-16 (F-08; 005 contracts/openapi.yaml, data-model.md §2).

EP-14 and EP-16 are open to every allow-listed role (FR-038); EP-15 is CONTROLLER or REVIEWER
(F-12.R4). The decision flow lives in `app.services.review`.
"""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.auth import require_role
from app.core.config import get_settings
from app.core.db import Repo, get_repo
from app.core.deps import get_notifier
from app.core.errors import InvalidReviewAction, NotFound
from app.core.faults import Fault, active_fault
from app.core.storage import INPUTS_BUCKET, BlobStore, get_blobstore
from app.domain.enums import (
    PRINTED_ROW_SLOTS,
    AuditChannel,
    ReviewAction,
    ReviewKind,
    ReviewReason,
    ReviewStatus,
    Role,
    Source,
)
from app.domain.models import AllowedUser, Cell, PageImageLink, ResolveRequest, ResolveResponse
from app.services import review
from app.services.telegram import Notifier

router = APIRouter()

RepoDep = Annotated[Repo, Depends(get_repo)]
BlobsDep = Annotated[BlobStore, Depends(get_blobstore)]
NotifierDep = Annotated[Notifier, Depends(get_notifier)]
Decider = Annotated[AllowedUser, Depends(require_role(Role.CONTROLLER, Role.REVIEWER))]
FaultDep = Annotated[Fault | None, Depends(active_fault)]

PAGE_IMAGE_URL_S = 300
"""EP-16: the page image link is valid 5 minutes (FR-003)."""


class ReviewCard(BaseModel):
    """EP-14 item: a `ReviewItem` whose `context` carries the view fields (R3, C25)."""

    id: UUID
    run_id: UUID
    kind: ReviewKind
    item_key: str
    grn_no: int | None
    source_row_id: str | None
    field: str | None
    reason: ReviewReason
    read_a: Cell | None
    read_b: Cell | None
    context: dict[str, Any]
    status: ReviewStatus
    resolved_value: str | None
    resolved_by: str | None
    resolved_at: datetime | None
    note: str | None


class ResolveBody(BaseModel):
    """EP-15 body as sent; its rules are checked as `ResolveRequest` (422, not 400)."""

    model_config = ConfigDict(extra="forbid")

    action: ReviewAction
    value: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=500)


def _request(body: ResolveBody) -> ResolveRequest:
    try:
        request = ResolveRequest.model_validate(body.model_dump())
    except ValidationError as exc:
        field = "note" if body.action is ReviewAction.CANNOT_DETERMINE else "value"
        raise InvalidReviewAction(field, exc.errors()[0]["msg"]) from None
    return request


def _channel(request: Request) -> AuditChannel:
    """WEB for a call from the app's browser origin, else API (data-model.md §8)."""
    origin = request.headers.get("origin")
    return AuditChannel.WEB if origin in get_settings().cors_origin_list else AuditChannel.API


@router.get("/runs/{id}/review-items", response_model=list[ReviewCard])
def list_review_items(
    id: UUID, repo: RepoDep, status: Annotated[ReviewStatus | None, Query()] = None
) -> list[ReviewCard]:
    """EP-14: card order (F-08.R2, the repo's order), each with its card context (R3)."""
    repo.get_run(id)
    return [
        ReviewCard(**{**item.model_dump(), "context": review.review_card_context(repo, item)})
        for item in repo.list_review_items(id, status)
    ]


@router.post("/review-items/{item_id}/resolve", response_model=ResolveResponse)
def resolve_review_item(
    item_id: UUID,
    body: ResolveBody,
    request: Request,
    user: Decider,
    repo: RepoDep,
    blobs: BlobsDep,
    notifier: NotifierDep,
    fault: FaultDep,
) -> ResolveResponse:
    """EP-15 (F-08.R4 to R8, FM-09): decide, re-reconcile, return the item and the run."""
    return review.resolve(
        repo, blobs, notifier, item_id, _request(body), user, _channel(request), fault=fault
    )


@router.get("/runs/{id}/pages/{page_no}/image", response_model=PageImageLink)
def page_image(
    id: UUID, page_no: Annotated[int, Path(ge=1, le=10)], repo: RepoDep, blobs: BlobsDep
) -> PageImageLink:
    """EP-16 (FR-003): a 5-minute signed link and read A's table edges for row highlighting."""
    repo.get_run(id)
    files = [f for f in repo.list_source_files(id, Source.REGISTER) if f.page_no == page_no]
    if not files:
        raise NotFound("page", page_no)
    url, expires_at = blobs.signed_download_url(
        INPUTS_BUCKET, files[0].storage_path, PAGE_IMAGE_URL_S
    )
    read = review.page_read_a(repo, id, page_no)
    return PageImageLink(
        page_no=page_no,
        signed_url=url,
        expires_at=expires_at,
        table_top=None if read is None else read.table_top,
        table_bottom=None if read is None else read.table_bottom,
        slots=PRINTED_ROW_SLOTS,
    )
