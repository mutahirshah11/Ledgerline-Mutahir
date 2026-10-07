"""Domain errors raised by the Repo and BlobStore (contracts/repo-interface.md G4 to G7), and
the API error model and handlers (004 data-model.md §1, blueprint Section 11, C17, FR-026)."""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.domain.enums import ErrorCode, Stage

logger = logging.getLogger("ledgerline.api")


class LedgerlineError(Exception):
    """Base for every domain error."""


class NotFound(LedgerlineError):
    """G4: an unknown id was requested."""

    def __init__(self, entity: str, id: object) -> None:
        super().__init__(f"{entity} not found: {id}")
        self.entity = entity
        self.id = id


class UniqueViolation(LedgerlineError):
    """G5: a unique key already exists."""

    def __init__(self, constraint: str) -> None:
        super().__init__(f"unique constraint violated: {constraint}")
        self.constraint = constraint


class StateConflict(LedgerlineError):
    """G6: a compare-and-set precondition failed; carries the current record."""

    def __init__(self, current: Any) -> None:
        super().__init__("state conflict")
        self.current = current


class AppendOnlyViolation(LedgerlineError):
    """An update or delete was attempted on an append-only record."""

    def __init__(self, entity: str) -> None:
        super().__init__(f"{entity} is append-only")
        self.entity = entity


class UpstreamFailed(LedgerlineError):
    """G7: a backend service still failed after retries.

    `stage` and `page_no` (003 E7) feed the alert text "Stage: extract (page 2)"; `attempts`
    feeds the API error details (004 data-model.md §1).
    """

    def __init__(
        self,
        service: str,
        detail: str,
        *,
        stage: str | None = None,
        page_no: int | None = None,
        attempts: int | None = None,
    ) -> None:
        super().__init__(f"{service} failed: {detail}")
        self.service = service
        self.detail = detail
        self.stage = stage
        self.page_no = page_no
        self.attempts = attempts


class PageUnreadable(LedgerlineError):
    """003 E4 (FM-06): more than half of a page's merged cells are illegible."""

    def __init__(self, page_no: int, illegible: int, total: int) -> None:
        super().__init__(f"Page {page_no} could not be read, retake the photo")
        self.page_no = page_no
        self.illegible = illegible
        self.total = total


class InvalidReadOutput(LedgerlineError):
    """003 E5 (FM-05): a model answer that is not valid read JSON. Retryable; internal only."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"invalid read output: {detail}")
        self.detail = detail


# --- Phase 4 errors (005 data-model.md §1, E1) ----------------------------------------------


class InvalidReviewAction(LedgerlineError):
    """F-08 AC4/AC5: a review action/value does not fit the item's kind, or a value fails its
    validator (data-model.md §1.1, §1.2). Maps to 422 INVALID_REVIEW_ACTION, stage review."""

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"invalid review action on {field}: {reason}")
        self.field = field
        self.reason = reason


class ItemAlreadyResolved(LedgerlineError):
    """FM-09: a second reviewer raced the first; carries the already-decided item. Maps to 409
    ITEM_ALREADY_RESOLVED, stage review."""

    def __init__(self, item: Any) -> None:
        super().__init__("review item is already resolved")
        self.item = item


class ConfirmRequired(LedgerlineError):
    """EP-17 (research R14): finalise requires `{"confirm": true}`. Maps to 400 CONFIRM_REQUIRED,
    stage finalise."""

    def __init__(self) -> None:
        super().__init__("finalise requires confirm: true")


# --- API error model (004 data-model.md §1) -------------------------------------------------


class ApiError(LedgerlineError):
    """An error the API returns as `{"error": {"code", "message", "stage", "details"}}`.

    `message` is plain language and never carries secrets; `details` must be JSON-safe.
    """

    def __init__(
        self,
        code: ErrorCode,
        http_status: int,
        message: str,
        stage: Stage | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.message = message
        self.stage = stage
        self.details: dict[str, Any] = details if details is not None else {}


def error_response(err: ApiError) -> JSONResponse:
    """The single error body of Section 11."""
    body = {
        "error": {
            "code": err.code.value,
            "message": err.message,
            "stage": err.stage.value if err.stage is not None else None,
            "details": err.details,
        }
    }
    # RFC 6750: a 401 names the scheme the client must use.
    headers = {"WWW-Authenticate": "Bearer"} if err.http_status == 401 else None
    return JSONResponse(status_code=err.http_status, content=body, headers=headers)


def _field(current: Any, name: str) -> Any:
    value = current.get(name) if isinstance(current, dict) else getattr(current, name, None)
    return getattr(value, "value", value)


_STAGES = frozenset(s.value for s in Stage)

# Framework HTTP errors (unknown route, bad method, auth) keep the one error shape.
_HTTP_CODES = {
    401: ErrorCode.UNAUTHENTICATED,
    403: ErrorCode.NOT_ALLOWED,
    404: ErrorCode.NOT_FOUND,
}

_UNEXPECTED = "Unexpected error"


def _to_api_error(exc: Exception) -> ApiError:
    """Map a known exception to its ApiError (data-model.md §1 table)."""
    if isinstance(exc, ApiError):
        return exc
    if isinstance(exc, NotFound):
        return ApiError(ErrorCode.NOT_FOUND, 404, f"{exc.entity} not found")
    if isinstance(exc, StateConflict):
        conflict = {
            "status": _field(exc.current, "status"),
            "failed_stage": _field(exc.current, "failed_stage"),
        }
        return ApiError(
            ErrorCode.RUN_STATE_CONFLICT, 409, "The run changed state; reload it", details=conflict
        )
    if isinstance(exc, UniqueViolation):
        # Call sites translate this (e.g. DUPLICATE_RUN with run_id); reaching here is a gap.
        # The constraint name is internal and never leaves the log.
        logger.warning("untranslated UniqueViolation: %s", exc.constraint)
        return ApiError(ErrorCode.RUN_STATE_CONFLICT, 409, "This conflicts with existing data")
    if isinstance(exc, UpstreamFailed):
        details: dict[str, Any] = {"service": exc.service}
        if exc.attempts is not None:
            details["attempts"] = exc.attempts
        if exc.page_no is not None:
            details["page_no"] = exc.page_no
        stage = Stage(exc.stage) if exc.stage is not None and exc.stage in _STAGES else None
        return ApiError(
            ErrorCode.UPSTREAM_FAILED, 502, f"{exc.service} failed after retries", stage, details
        )
    if isinstance(exc, InvalidReviewAction):
        return ApiError(
            ErrorCode.INVALID_REVIEW_ACTION,
            422,
            "This action is not valid for this item",
            Stage.REVIEW,
            details={"field": exc.field, "reason": exc.reason},
        )
    if isinstance(exc, ItemAlreadyResolved):
        item = exc.item.model_dump(mode="json") if hasattr(exc.item, "model_dump") else exc.item
        return ApiError(
            ErrorCode.ITEM_ALREADY_RESOLVED,
            409,
            "This item was already decided",
            Stage.REVIEW,
            details={"item": item},
        )
    if isinstance(exc, ConfirmRequired):
        return ApiError(
            ErrorCode.CONFIRM_REQUIRED, 400, "Confirm the finalise to proceed", Stage.FINALISE
        )
    if isinstance(exc, RequestValidationError):
        fields = [
            {"loc": list(e.get("loc", ())), "msg": str(e.get("msg", ""))} for e in exc.errors()
        ]
        return ApiError(
            ErrorCode.BAD_REQUEST, 400, "The request is not valid", details={"fields": fields}
        )
    if isinstance(exc, StarletteHTTPException) and exc.status_code < 500:
        code = _HTTP_CODES.get(exc.status_code, ErrorCode.BAD_REQUEST)
        return ApiError(code, exc.status_code, str(exc.detail))
    # 5xx HTTPException and anything else: the detail stays in the log, never in the body.
    logger.error("unexpected error", exc_info=exc)
    # `stage` stays null here: the stage services raise their own staged 500s (`_unexpected`).
    return ApiError(ErrorCode.INTERNAL_ERROR, 500, _UNEXPECTED)


async def _handle(request: Request, exc: Exception) -> JSONResponse:
    return error_response(_to_api_error(exc))


def install_error_handlers(app: FastAPI) -> None:
    """Route every error through the Section 11 shape; unknown ones become 500 INTERNAL_ERROR."""
    for exc_type in (
        ApiError,
        NotFound,
        StateConflict,
        UniqueViolation,
        UpstreamFailed,
        InvalidReviewAction,
        ItemAlreadyResolved,
        ConfirmRequired,
        RequestValidationError,
        StarletteHTTPException,
        Exception,
    ):
        app.add_exception_handler(exc_type, _handle)
