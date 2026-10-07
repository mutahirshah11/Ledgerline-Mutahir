"""Stage services callable without HTTP (004 plan.md, data-model.md §2 to §4).

Upload stage (F-01): `create_run` (EP-03), `request_uploads` (EP-06), `confirm_uploads` (EP-07).
The upload window starts at the latest `upload` `STARTED` event (Clarification 5, research R2).
Validate (EP-08), extract (EP-09, Phase 2 `extract_page`) and reconcile (EP-10, Phase 1
`reconcile_run`) follow the stage rules of data-model.md §3.
"""

import hashlib
import logging
import time
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.core import clock
from app.core.config import Settings, get_settings
from app.core.db import Repo
from app.core.errors import ApiError, NotFound, PageUnreadable, StateConflict, UpstreamFailed
from app.core.storage import INPUTS_BUCKET, BlobStore
from app.domain.enums import (
    AuditAction,
    AuditChannel,
    ErrorCode,
    Fault,
    Outcome,
    ProofStatus,
    ReadVariant,
    Role,
    RunEventStatus,
    RunStatus,
    Source,
    Stage,
)
from app.domain.ids import parse_month, run_key
from app.domain.models import (
    AllowedUser,
    PageExtractionResult,
    PageManifestEntry,
    PageRead,
    RegisterPageInput,
    Run,
    Sha256Hex,
    SourceFile,
)
from app.pipeline.engine import (
    RECONCILABLE,
    EngineInputs,
    open_review_count,
    outcome_counts_of,
    reconcile_run,
)
from app.pipeline.extract import (
    RetryInfo,
    extract_page,
    merge_reads,
    page_result,
    parse_read,
    register_input,
)
from app.pipeline.model_client import EXTRACT_FAULTS, FaultModelClient, ModelClient
from app.pipeline.normalise import load_mappings
from app.pipeline.validate import RegisterFile, ValidationProblem, validate_files
from app.services.alerts import stage_failed_alert
from app.services.audit import audit
from app.services.runlog import StepLog, stage_step
from app.services.telegram import Notifier

Now = Callable[[], datetime]

log = logging.getLogger(__name__)

KB = 1024
MB = 1024 * KB
REGISTER_MIN_BYTES = 20 * KB
MAX_FILE_BYTES = 10 * MB
MAX_PAGES = 10
EXTENSIONS: dict[Source, frozenset[str]] = {
    Source.REGISTER: frozenset({".jpg", ".jpeg", ".png"}),
    Source.ERP: frozenset({".csv"}),
    Source.SHEET: frozenset({".xlsx"}),
}
"""Section 7 / data-model.md §2 Uploads (F-01.R3, C19)."""


class UploadFileIn(BaseModel):
    """One declared file of EP-06 (data-model.md §2)."""

    model_config = ConfigDict(frozen=True)

    source: Source
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=1)
    sha256: Sha256Hex
    page_no: int | None = Field(default=None, ge=1, le=MAX_PAGES)


class UploadSlot(BaseModel):
    file_id: UUID
    source: Source
    filename: str
    page_no: int | None
    storage_path: str
    signed_url: str


class ConfirmResult(BaseModel):
    """EP-07 result: the run and the page labels new to / known by the manifest (F-01.R6)."""

    run: Run
    new_pages: list[int]
    known_pages: list[int]


# --- EP-03 -----------------------------------------------------------------------------


def create_run(repo: Repo, user: AllowedUser, month: str, *, now: Now = clock.now) -> Run:
    """F-01.R1: a `CREATED` run for a real `YYYY-MM` month; 400 `BAD_MONTH` otherwise."""
    try:
        parse_month(month)
    except ValueError:
        raise ApiError(
            ErrorCode.BAD_MONTH, 400, f"Month must be YYYY-MM, got {month!r}", Stage.UPLOAD
        ) from None
    at = now()
    return repo.create_run(
        Run(
            id=uuid4(),
            month=month,
            status=RunStatus.CREATED,
            run_key=None,
            failed_stage=None,
            error_message=None,
            scenario_now=at,
            created_by=user.email,
            created_at=at,
            finalised_at=None,
            finalised_by=None,
        )
    )


# --- EP-06 -----------------------------------------------------------------------------


def _ext(filename: str) -> str:
    return PurePosixPath(filename).suffix.lower()


def _check_files(files: Sequence[UploadFileIn]) -> None:
    """F-01.R3: 400 `MISSING_SOURCE`, else 422 `FILE_RULES` listing every violation."""
    counts = Counter(f.source for f in files)
    missing = [s.value for s in Source if counts[s] == 0]
    if missing:
        raise ApiError(
            ErrorCode.MISSING_SOURCE,
            400,
            f"Missing source file: {', '.join(missing)}",
            Stage.UPLOAD,
            {"missing": missing},
        )
    problems: list[dict[str, Any]] = []

    def bad(filename: str | None, message: str) -> None:
        problems.append({"filename": filename, "message": message})

    for source in (Source.ERP, Source.SHEET):
        if counts[source] > 1:
            bad(None, f"Exactly one {source.value} file is allowed, got {counts[source]}")
    if counts[Source.REGISTER] > MAX_PAGES:
        bad(None, f"At most {MAX_PAGES} register pages, got {counts[Source.REGISTER]}")
    for f in files:
        if _ext(f.filename) not in EXTENSIONS[f.source]:
            allowed = ", ".join(sorted(EXTENSIONS[f.source]))
            bad(f.filename, f"{f.source.value} file must be {allowed}")
        if f.source is Source.REGISTER and f.size_bytes < REGISTER_MIN_BYTES:
            bad(f.filename, "Register image is smaller than 20 KB")
        if f.size_bytes > MAX_FILE_BYTES:
            bad(f.filename, "File is larger than 10 MB")
        if f.page_no is not None and f.source is not Source.REGISTER:
            bad(f.filename, "Only register pages have a page number")
    for sha, n in Counter(f.sha256 for f in files).items():
        if n > 1:
            names = [f.filename for f in files if f.sha256 == sha]
            bad(None, f"The same file is declared {n} times: {', '.join(names)}")
    for page, n in Counter(f.page_no for f in files if f.page_no is not None).items():
        if n > 1:
            bad(None, f"Page {page} is given {n} times")
    if problems:
        raise ApiError(
            ErrorCode.CONTRACT_VIOLATION,
            422,
            "The files break the upload rules",
            Stage.UPLOAD,
            {"problem": "FILE_RULES", "violations": problems},
        )


def _page_numbers(files: Sequence[UploadFileIn]) -> list[int | None]:
    """Given numbers kept; the rest take the lowest free numbers in list order (F-01.R2)."""
    used = {f.page_no for f in files if f.page_no is not None}
    free = (n for n in range(1, MAX_PAGES + 1) if n not in used)
    out: list[int | None] = []
    for f in files:
        if f.source is not Source.REGISTER:
            out.append(None)
        else:
            out.append(f.page_no if f.page_no is not None else next(free))
    return out


def storage_path(run_id: UUID, source: Source, sha256: str, page_no: int | None, ext: str) -> str:
    """Section 10 paths in bucket `inputs`."""
    if source is Source.ERP:
        return f"{run_id}/erp.csv"
    if source is Source.SHEET:
        return f"{run_id}/sheet.xlsx"
    suffix = ".png" if ext == ".png" else ".jpg"
    return f"{run_id}/register/p{page_no}-{sha256}{suffix}"


def _require_created(run: Run) -> None:
    if run.status is not RunStatus.CREATED:
        raise StateConflict(run)


def request_uploads(
    repo: Repo,
    blobs: BlobStore,
    run: Run,
    files: Sequence[UploadFileIn],
    *,
    now: Now = clock.now,
) -> list[UploadSlot]:
    """EP-06: rules first (nothing written on failure), then one record and one signed URL per
    file. A repeat request reuses matching records and replaces stale ones (research R5); its
    `upload` `STARTED` event restarts the 10-minute window."""
    _require_created(run)
    _check_files(files)
    existing = {f.sha256: f for f in repo.list_source_files(run.id)}
    wanted: list[SourceFile] = []
    for spec, page_no in zip(files, _page_numbers(files), strict=True):
        wanted.append(
            SourceFile(
                id=uuid4(),
                run_id=run.id,
                source=spec.source,
                filename=spec.filename,
                storage_path=storage_path(
                    run.id, spec.source, spec.sha256, page_no, _ext(spec.filename)
                ),
                sha256=spec.sha256,
                size_bytes=spec.size_bytes,
                page_no=page_no,
                confirmed_at=None,
            )
        )
    keep: dict[str, SourceFile] = {}
    for want in wanted:
        old = existing.get(want.sha256)
        same = old is not None and old.model_dump(
            exclude={"id", "confirmed_at"}
        ) == want.model_dump(exclude={"id", "confirmed_at"})
        if old is not None and same:
            keep[want.sha256] = old
    for sha, old in existing.items():
        if sha not in keep:
            repo.delete_source_file(old.id)
            if not any(w.storage_path == old.storage_path for w in wanted):
                blobs.delete(INPUTS_BUCKET, old.storage_path)
    stored = [keep.get(w.sha256) or repo.add_source_file(w) for w in wanted]
    StepLog(repo, run.id, Stage.UPLOAD, now).write(RunEventStatus.STARTED, {"files": len(stored)})
    expires = get_settings().UPLOAD_WINDOW_S
    _require_created(repo.get_run(run.id))  # a concurrent confirm must not race the deletes below
    slots: list[UploadSlot] = []
    for f in stored:
        # Signed upload URLs are `upsert=false`: a path already holding an object (a fixed ERP
        # or sheet path re-requested, or a kept file re-requested) must be cleared first, or the
        # browser's upload gets a 409 (research R5). `delete` is a no-op when nothing is there.
        blobs.delete(INPUTS_BUCKET, f.storage_path)
        slots.append(
            UploadSlot(
                file_id=f.id,
                source=f.source,
                filename=f.filename,
                page_no=f.page_no,
                storage_path=f.storage_path,
                signed_url=blobs.signed_upload_url(
                    INPUTS_BUCKET, f.storage_path, expires_s=expires
                ),
            )
        )
    return slots


# --- EP-07 -----------------------------------------------------------------------------


def _pages(repo: Repo, run: Run) -> tuple[list[int], list[int]]:
    """(new, known) page labels: new when this run first put the page in the manifest."""
    new: list[int] = []
    known: list[int] = []
    for f in sorted(repo.list_source_files(run.id, Source.REGISTER), key=lambda f: f.page_no or 0):
        page = repo.get_page(f.sha256)
        assert f.page_no is not None
        (new if page is None or page.first_seen_run_id == run.id else known).append(f.page_no)
    return new, known


def _issued_at(repo: Repo, run_id: UUID) -> datetime | None:
    for event in repo.list_run_events(
        run_id
    ):  # newest first, ties newest insertion (same rule as EP-05)
        if event.stage is Stage.UPLOAD and event.status is RunEventStatus.STARTED:
            return event.at
    return None


def _ms(start: datetime | None, end: datetime) -> int | None:
    return None if start is None else round((end - start).total_seconds() * 1000)


def _file_ref(f: SourceFile, problem: str) -> dict[str, Any]:
    return {
        "file_id": str(f.id),
        "filename": f.filename,
        "source": f.source.value,
        "problem": problem,
    }


def _verify(
    blobs: BlobStore, files: list[SourceFile], issued: datetime | None, at: datetime
) -> list[dict[str, Any]]:
    """FM-01 / FM-02 for every file. A mismatched object is deleted and its record kept, so a
    retry without re-uploading reports FILE_MISSING (blueprint Section 25 FM-02)."""
    window = get_settings().UPLOAD_WINDOW_S
    if issued is None or (at - issued).total_seconds() > window:
        return [_file_ref(f, "UPLOAD_EXPIRED") for f in files]
    problems: list[dict[str, Any]] = []
    for f in files:
        try:
            data = blobs.get(INPUTS_BUCKET, f.storage_path)
        except NotFound:
            problems.append(_file_ref(f, "FILE_MISSING"))
            continue
        if hashlib.sha256(data).hexdigest() != f.sha256:
            blobs.delete(INPUTS_BUCKET, f.storage_path)
            problems.append(_file_ref(f, "HASH_MISMATCH"))
    return problems


def _fail(step: StepLog, issued: datetime | None, at: datetime, err: ApiError) -> ApiError:
    step.write(
        RunEventStatus.FAILED,
        {
            "error": err.code.value,
            **({"problem": err.details["problem"]} if "problem" in err.details else {}),
        },
        _ms(issued, at),
    )
    return err


def _files_changed(
    repo: Repo, notifier: Notifier | None, moved: Run, step: StepLog, now: Now
) -> StateConflict:
    """EP-06 raced this confirm: the run's files are not the set just confirmed (added or
    removed between the list and the move to `UPLOADED`; none can change after it, 0002
    triggers). The run key and checks no longer hold: `FAILED` at `upload`, one ops alert,
    409; the run must be created again."""
    message = "The run's files changed while they were confirmed; create the run again"
    step.write(RunEventStatus.FAILED, {"error": ErrorCode.RUN_STATE_CONFLICT.value})
    failed = repo.transition_run(
        moved.id,
        RunStatus.FAILED,
        expected={RunStatus.UPLOADED},
        failed_stage=Stage.UPLOAD,
        error_message=message,
    )
    if notifier is not None:
        err = ApiError(ErrorCode.RUN_STATE_CONFLICT, 409, message, Stage.UPLOAD)
        stage_failed_alert(repo, notifier, failed, Stage.UPLOAD, err, now=now)
    return StateConflict(failed)


def confirm_uploads(
    repo: Repo,
    blobs: BlobStore,
    run: Run,
    user: AllowedUser,
    *,
    notifier: Notifier | None = None,
    now: Now = clock.now,
) -> ConfirmResult:
    """EP-07 (F-01.R4 to R7). Past `CREATED` it returns the run as is without writes; a
    `FINALISED` run is 409. Audit `RUN_CREATED` and `FILE_UPLOADED` × n only on the transition.
    After the transition the run's files are listed again: a set changed by a concurrent EP-06
    fails the run at `upload` (`_files_changed`; the route always passes `notifier`)."""
    if run.status is RunStatus.FINALISED:
        raise StateConflict(run)
    if run.status is not RunStatus.CREATED:
        new, known = _pages(repo, run)
        return ConfirmResult(run=run, new_pages=new, known_pages=known)
    at = now()
    issued = _issued_at(repo, run.id)
    step = StepLog(repo, run.id, Stage.UPLOAD, now)
    files = repo.list_source_files(run.id)
    present = {f.source for f in files}
    missing = [s.value for s in Source if s not in present]
    if missing:
        raise ApiError(
            ErrorCode.MISSING_SOURCE,
            400,
            f"Missing source file: {', '.join(missing)}; request upload links again",
            Stage.UPLOAD,
            {"missing": missing},
        )
    problems = _verify(blobs, files, issued, at)
    if problems:
        kinds = {p["problem"] for p in problems}
        problem = next(k for k in ("UPLOAD_EXPIRED", "FILE_MISSING", "HASH_MISMATCH") if k in kinds)
        names = ", ".join(p["filename"] for p in problems)
        raise _fail(
            step,
            issued,
            at,
            ApiError(
                ErrorCode.CONTRACT_VIOLATION,
                422,
                f"Upload not verified ({problem}): {names}",
                Stage.UPLOAD,
                {"problem": problem, "files": problems},
            ),
        )
    key = run_key(run.month, [f.sha256 for f in files])
    duplicate = repo.find_finalised_run_by_key(key)
    if duplicate is not None:
        raise _fail(
            step,
            issued,
            at,
            ApiError(
                ErrorCode.DUPLICATE_RUN,
                409,
                "This exact month and files were already finalised",
                Stage.UPLOAD,
                {"run_id": str(duplicate.id)},
            ),
        )
    for f in sorted(files, key=lambda f: f.page_no or 0):
        if f.source is Source.REGISTER:
            assert f.page_no is not None
            repo.add_page_if_new(
                PageManifestEntry(
                    sha256=f.sha256,
                    month=run.month,
                    page_no=f.page_no,
                    first_seen_run_id=run.id,
                    first_seen_at=at,
                )
            )
    confirmed = [repo.confirm_source_file(f.id, at) for f in files]
    try:
        moved = repo.transition_run(
            run.id, RunStatus.UPLOADED, expected={RunStatus.CREATED}, run_key=key
        )
    except StateConflict as conflict:  # a concurrent confirm won; answer as a repeat call
        current: Run = conflict.current
        if current.status is RunStatus.FINALISED:
            raise
        new, known = _pages(repo, current)
        return ConfirmResult(run=current, new_pages=new, known_pages=known)
    now_files = repo.list_source_files(run.id)
    if {f.id for f in now_files} != {f.id for f in confirmed} or any(
        f.confirmed_at is None for f in now_files
    ):
        raise _files_changed(repo, notifier, moved, step, now)
    new, known = _pages(repo, moved)
    audit(
        repo,
        user.email,
        AuditAction.RUN_CREATED,
        "run",
        str(run.id),
        run.id,
        after={"month": run.month, "status": moved.status.value, "run_key": key},
        channel=AuditChannel.WEB,
        now=now,
    )
    for f in confirmed:
        audit(
            repo,
            user.email,
            AuditAction.FILE_UPLOADED,
            "source_file",
            str(f.id),
            run.id,
            after={
                "source": f.source.value,
                "filename": f.filename,
                "sha256": f.sha256,
                "size_bytes": f.size_bytes,
                "page_no": f.page_no,
            },
            channel=AuditChannel.WEB,
            now=now,
        )
    step.write(
        RunEventStatus.OK,
        {"files": len(files), "new_pages": new, "known_pages": known},
        _ms(issued, at),
    )
    return ConfirmResult(run=moved, new_pages=new, known_pages=known)


# --- read views ------------------------------------------------------------------------


def run_outcome_counts(repo: Repo, run_id: UUID) -> dict[Outcome, int]:
    """data-model.md §2 `OutcomeCounts` of a stored run (engine `OutcomeCounts.flat`).

    Two reads (groups, rows); EP-04 uses the batched `Repo.count_outcomes` instead (006)."""
    return outcome_counts_of(repo.list_source_rows(run_id), repo.list_groups(run_id)).flat()


# --- EP-08 -----------------------------------------------------------------------------

_VALIDATE_PAST = frozenset(
    {
        RunStatus.VALIDATED,
        RunStatus.EXTRACTING,
        RunStatus.EXTRACTED,
        RunStatus.RECONCILED,
        RunStatus.AWAITING_REVIEW,
        RunStatus.READY,
    }
)
"""Statuses the run reaches only after passing validate (data-model.md §3 EP-08 row);
repeating the call here is `{ok: true}`, no writes. `FINALISED` is excluded on purpose: any
stage call on a finalised run is 409 (data-model.md §3, "any stage" row)."""


def _source_bytes(repo: Repo, blobs: BlobStore, run_id: UUID, source: Source) -> bytes:
    """NotFound (the blob is missing) or ValueError (not exactly one confirmed file of
    `source`, a broken upload state) on an unpack failure (item 4: this is the API's own fault,
    not a contract problem, so the caller maps it to 500, not 422)."""
    (file,) = repo.list_source_files(run_id, source)
    return blobs.get(INPUTS_BUCKET, file.storage_path)


def _register_files(repo: Repo, blobs: BlobStore, run_id: UUID) -> list[RegisterFile]:
    files = sorted(repo.list_source_files(run_id, Source.REGISTER), key=lambda f: f.page_no or 0)
    return [
        RegisterFile(f.page_no or 0, f.filename, blobs.get(INPUTS_BUCKET, f.storage_path))
        for f in files
    ]


ERROR_MESSAGE_LIMIT = 500
PROBLEMS_SHOWN = 200
"""Cap on EP-08's 422 `details.problems`; `details.truncated` counts the rest."""
"""Hard cap on `runs.error_message` (item 3); long enough to show a few problems in full."""
_SUMMARY_SHOWN = 3
"""How many problems the run's `error_message` and the ops alert reason show in full."""


def _capped_list(texts: Sequence[str], shown: int) -> str:
    """The first `shown` items, then `"; and N more"` for the rest; one summary line."""
    head = list(texts[:shown])
    rest = len(texts) - len(head)
    if rest > 0:
        head.append(f"and {rest} more")
    return "; ".join(head)


def _problem_summary(problems: Sequence[ValidationProblem]) -> str:
    """`runs.error_message` (item 3): plain-language problem text, capped to
    `ERROR_MESSAGE_LIMIT` characters (a hard limit, not a clean cut)."""
    n = len(problems)
    label = "problem" if n == 1 else "problems"
    text = f"{n} {label}: " + _capped_list([p.message for p in problems], _SUMMARY_SHOWN)
    return text[:ERROR_MESSAGE_LIMIT]


def _alert_problem_text(p: ValidationProblem) -> str:
    """One problem named by its code and location only — never a cell value, a file's own
    text, or anything else read from the file (contracts/alerts.md "no file contents";
    item 2). `location.worksheet` here is the month's own expected sheet name, derived from
    the run, not something read from the workbook."""
    source = p.source.value
    loc = p.location
    if loc is None:
        return f"{source} {p.code.value}"
    if loc.line is not None:  # ERP: "ERP line 5 qty BAD_NUMBER"
        words = [source, f"line {loc.line}"]
        if loc.column:
            words.append(loc.column)
        words.append(p.code.value)
        return " ".join(words)
    words = [source, p.code.value]  # sheet / register: "SHEET MISSING_WORKSHEET Sep-2026"
    if loc.worksheet:
        words.append(loc.worksheet)
    if loc.column:
        words.append(loc.column)
    if loc.row is not None:
        words.append(f"row {loc.row}")
    if loc.page_no is not None:
        words.append(f"page {loc.page_no}")
    return " ".join(words)


def _alert_reason(problems: Sequence[ValidationProblem]) -> str:
    """The ops alert's "Error:" reason (item 2): counts and codes/locations only, capped like
    `_problem_summary` but never carrying a problem's own (data-derived) message text."""
    n = len(problems)
    label = "problem" if n == 1 else "problems"
    body = _capped_list([_alert_problem_text(p) for p in problems], _SUMMARY_SHOWN)
    return f"{n} {label} ({body})"


def validate_stage(
    repo: Repo,
    blobs: BlobStore,
    notifier: Notifier,
    run: Run,
    user: AllowedUser,
    *,
    now: Now = clock.now,
) -> list[ValidationProblem]:
    """EP-08 (F-02.R5, Clarification 3, data-model.md §3). Only a run `UPLOADED` is checked;
    `VALIDATED` or later repeats as `{ok: true}` with no writes; any other status (including
    `CREATED` and `FAILED` at any stage) is 409 `RUN_STATE_CONFLICT` — validate has no retry
    from `FAILED` (FR-024): a run failed at validate must be re-created, not retried. The role
    guard lets `OPS` through on a `FAILED` run as a pre-check (contracts/auth.md); this service
    still returns 409 for `OPS` the same as `CONTROLLER`, since there is nothing to retry."""
    if run.status in _VALIDATE_PAST:
        return []
    if run.status is not RunStatus.UPLOADED:
        raise StateConflict(run)
    with stage_step(repo, run.id, Stage.VALIDATE, now=now):
        try:
            return _validate_uploaded(repo, blobs, notifier, run, user, now)
        except (ApiError, StateConflict, UpstreamFailed):
            raise
        except Exception as exc:  # constitution VIII: one alert, generic 500, no move to FAILED
            stage_failed_alert(repo, notifier, run, Stage.VALIDATE, exc, now=now)
            raise _unexpected(Stage.VALIDATE) from exc


def _validate_uploaded(
    repo: Repo, blobs: BlobStore, notifier: Notifier, run: Run, user: AllowedUser, now: Now
) -> list[ValidationProblem]:
    """EP-08 on an `UPLOADED` run, inside its `validate` step."""
    try:
        erp = _source_bytes(repo, blobs, run.id, Source.ERP)
        sheet = _source_bytes(repo, blobs, run.id, Source.SHEET)
        registers = _register_files(repo, blobs, run.id)
    except (NotFound, ValueError) as exc:
        # item 4: the run's own inputs are missing or inconsistent in storage — the API's
        # fault, not the contract's. The run stays UPLOADED (no transition) so it can be
        # called again once the inputs are restored; one ops alert either way.
        raise _inputs_missing(repo, notifier, run, Stage.VALIDATE, exc, now) from exc
    problems = validate_files(run.month, erp, sheet, registers)
    if not problems:
        try:
            moved = repo.transition_run(run.id, RunStatus.VALIDATED, expected={RunStatus.UPLOADED})
        except StateConflict as conflict:
            current = conflict.current
            if current.status not in _VALIDATE_PAST:
                raise  # a genuine conflict (e.g. moved to FAILED): a real FAILED event
            return []  # lost the race to another validate call that already passed
        # Not atomic with the transition above: a crash between the two lines leaves the
        # run VALIDATED with no RUN_VALIDATED entry (item 9); accepted for this phase.
        audit(
            repo,
            user.email,
            AuditAction.RUN_VALIDATED,
            "run",
            str(run.id),
            run.id,
            after={"status": moved.status.value},
            channel=AuditChannel.WEB,
            now=now,
        )
        return []
    shown = [p.model_dump(mode="json") for p in problems[:PROBLEMS_SHOWN]]
    details: dict[str, Any] = {"problems": shown}
    if len(problems) > PROBLEMS_SHOWN:
        details["truncated"] = len(problems) - PROBLEMS_SHOWN
    err = ApiError(
        ErrorCode.CONTRACT_VIOLATION,
        422,
        "The inputs break the contract; see details.problems",
        Stage.VALIDATE,
        details,
    )
    failed = repo.transition_run(
        run.id,
        RunStatus.FAILED,
        expected={RunStatus.UPLOADED},
        failed_stage=Stage.VALIDATE,
        error_message=_problem_summary(problems),
    )
    alert_err = ApiError(ErrorCode.CONTRACT_VIOLATION, 422, _alert_reason(problems), Stage.VALIDATE)
    stage_failed_alert(repo, notifier, failed, Stage.VALIDATE, alert_err, now=now)
    raise err  # `stage_step` writes the FAILED run event on the way out


def _inputs_missing(
    repo: Repo, notifier: Notifier, run: Run, stage: Stage, exc: Exception, now: Now
) -> ApiError:
    """Infrastructure failure (a stored input is missing): 500, run state unchanged, one ops
    alert (as EP-08, item 4)."""
    broken = ApiError(ErrorCode.INTERNAL_ERROR, 500, "Run inputs are missing from storage", stage)
    stage_failed_alert(repo, notifier, run, stage, broken, now=now)
    return broken


def _unexpected(stage: Stage) -> ApiError:
    """The generic 500 of an unexpected error (constitution VIII: fail loudly, say nothing)."""
    return ApiError(ErrorCode.INTERNAL_ERROR, 500, "Unexpected error", stage)


def _internal_message(exc: BaseException) -> str:
    """The stored text of an unexpected error: its type name only, never its message."""
    return f"internal error ({type(exc).__name__})"


# --- EP-09 -----------------------------------------------------------------------------


class ExtractPageResult(BaseModel):
    """EP-09 `ExtractPageResponse` (data-model.md §2)."""

    page_no: int
    cached: bool
    rows: int
    review_cells: int
    disagreements: int


_EXTRACT_PAST = frozenset(
    {RunStatus.EXTRACTED, RunStatus.RECONCILED, RunStatus.AWAITING_REVIEW, RunStatus.READY}
)
"""Statuses past extract: a repeat EP-09 call returns the page result with no writes (§3)."""

Reader = Callable[[Callable[[RetryInfo], None] | None], PageExtractionResult]


def _register_file(repo: Repo, run_id: UUID, page_no: int) -> SourceFile:
    for f in repo.list_source_files(run_id, Source.REGISTER):
        if f.page_no == page_no:
            return f
    raise NotFound("page", page_no)


def _extract_ok_cached(repo: Repo, run_id: UUID) -> dict[int, bool]:
    """`detail.cached` of the run's newest extract OK event per page (data-model.md §2)."""
    found: dict[int, bool] = {}
    for e in repo.list_run_events(run_id):  # newest first
        page_no, cached = e.detail.get("page_no"), e.detail.get("cached")
        if e.stage is Stage.EXTRACT and e.status is RunEventStatus.OK and page_no not in found:
            if isinstance(page_no, int) and isinstance(cached, bool):
                found[page_no] = cached
    return found


def _stored_reads(repo: Repo, sha: str, settings: Settings) -> tuple[PageRead, PageRead] | None:
    """Reads A and B of a page under the current model and prompt, validated; None if either
    is missing. A record that no longer validates raises `InvalidReadOutput`."""
    records = [
        repo.get_extraction(sha, v, settings.EXTRACT_MODEL, settings.PROMPT_VERSION)
        for v in ReadVariant
    ]
    if records[0] is None or records[1] is None:
        return None
    a, b = (parse_read(r.result.model_dump(mode="json")) for r in (records[0], records[1]))
    return a, b


def _has_both_reads(repo: Repo, sha: str, settings: Settings) -> bool:
    return all(
        repo.get_extraction(sha, v, settings.EXTRACT_MODEL, settings.PROMPT_VERSION) is not None
        for v in ReadVariant
    )


def _repeat(
    reads: tuple[PageRead, PageRead], run: Run, file: SourceFile, cached: bool
) -> ExtractPageResult:
    """A repeat call's answer from the stored reads (Phase 2 `page_result`): no write, no
    model call (Section 11)."""
    result = page_result(
        *reads,
        run_id=run.id,
        run_month=run.month,
        page_no=file.page_no or 0,
        page_sha256=file.sha256,
    )
    return _page_result(result, cached)


def _check_extract_accepted(run: Run, user: AllowedUser) -> None:
    """data-model.md §3: `VALIDATED`, `EXTRACTING` or `FAILED(extract)`; OPS only the last
    (contracts/auth.md). Checked before any blob is fetched."""
    if run.status is RunStatus.FAILED and run.failed_stage is Stage.EXTRACT:
        return
    if user.role is Role.OPS or run.status not in (RunStatus.VALIDATED, RunStatus.EXTRACTING):
        raise StateConflict(run)


def _enter_extracting(repo: Repo, run: Run, user: AllowedUser) -> Run:
    """`VALIDATED` → `EXTRACTING`, or `FAILED(extract)` → `EXTRACTING` (retry); `EXTRACTING`
    stays. OPS may only retry: its only `expected` status is `FAILED`, so a run that left
    `FAILED` meanwhile is a 409, not a retry (contracts/auth.md)."""
    _check_extract_accepted(run, user)
    if run.status is RunStatus.EXTRACTING:
        return run
    expected = {RunStatus.FAILED} if run.status is RunStatus.FAILED else {RunStatus.VALIDATED}
    try:
        return repo.transition_run(run.id, RunStatus.EXTRACTING, expected=expected)
    except StateConflict as conflict:  # a concurrent page call moved it first
        current: Run = conflict.current
        if current.status is RunStatus.EXTRACTING and user.role is not Role.OPS:
            return current
        raise


def _finish_extracting(repo: Repo, run_id: UUID, settings: Settings) -> None:
    """`EXTRACTING` → `EXTRACTED` once every register page has an extract OK event in this run
    and both reads under the current model and prompt (data-model.md §3). Losing the race to
    a concurrent call is quiet: it either finished the run or failed it after this page
    succeeded."""
    done = _extract_ok_cached(repo, run_id)
    files = repo.list_source_files(run_id, Source.REGISTER)
    if not all(f.page_no in done and _has_both_reads(repo, f.sha256, settings) for f in files):
        return
    try:
        repo.transition_run(run_id, RunStatus.EXTRACTED, expected={RunStatus.EXTRACTING})
    except StateConflict as conflict:
        if conflict.current.status not in (RunStatus.EXTRACTED, RunStatus.FAILED):
            raise


def _page_result(result: PageExtractionResult, cached: bool) -> ExtractPageResult:
    return ExtractPageResult(
        page_no=result.page_no,
        cached=cached,
        rows=len(result.page.rows),
        review_cells=result.review_cells,
        disagreements=result.disagreements,
    )


def extract_stage(
    repo: Repo,
    blobs: BlobStore,
    notifier: Notifier,
    run: Run,
    page_no: int,
    user: AllowedUser,
    fault: Fault | None,
    *,
    client: ModelClient,
    sleep: Callable[[float], None] = time.sleep,
    settings: Settings | None = None,
    now: Now = clock.now,
) -> ExtractPageResult:
    """EP-09 (F-03, FR-019, FR-020, FR-024; data-model.md §3, §4).

    Status, role and the accept set are checked first (409), then the page (404). A page this
    run already extracted is answered from its stored reads with no write and no model call
    (Section 11); past extract, a missing read is 409. Otherwise the Phase 2 `extract_page`
    reads it (attempts and backoff from settings, the per-call timeout is the client's,
    NFR-03) inside `stage_step("extract", page_no)`: one `RETRY` event per retry the reader
    reports, the OK event detail `{page_no, cached}`, audit `PAGE_EXTRACTED`.
    `UpstreamFailed` (FM-04, FM-05) → 502, `PageUnreadable` (FM-06) → 422, any other error →
    generic 500 (constitution VIII); each sets `FAILED` at `extract` with one ops alert. An
    unexpected error in the lookups before the read is one ops alert and a generic 500, the
    run's state unchanged.
    `EXTRACT_TIMEOUT` and `EXTRACT_BAD_JSON` wrap the client; other faults are ignored here.
    """
    settings = settings or get_settings()
    if run.status is RunStatus.FINALISED or (
        user.role is Role.OPS and run.status is not RunStatus.FAILED
    ):
        raise StateConflict(run)
    if run.status not in _EXTRACT_PAST:
        _check_extract_accepted(run, user)
    try:
        file = _register_file(repo, run.id, page_no)
        done = _extract_ok_cached(repo, run.id)
        if run.status in _EXTRACT_PAST:
            return _repeat_past(repo, notifier, run, file, done, settings, now)
        repeat = page_no in done and _has_both_reads(repo, file.sha256, settings)
        image = b""
        if not repeat:
            try:
                image = blobs.get(INPUTS_BUCKET, file.storage_path)
            except NotFound as exc:
                raise _inputs_missing(repo, notifier, run, Stage.EXTRACT, exc, now) from exc
        run = _enter_extracting(repo, run, user)
    except (ApiError, NotFound, StateConflict, UpstreamFailed):
        raise  # 404 page, 409 state, 500 inputs missing, 502 storage: unchanged
    except Exception as exc:  # before the read: one alert, generic 500, state unchanged
        stage_failed_alert(repo, notifier, run, Stage.EXTRACT, exc, page_no, now=now)
        raise _unexpected(Stage.EXTRACT) from exc
    reader_client = FaultModelClient(client, fault) if fault in EXTRACT_FAULTS else client

    def read(on_retry: Callable[[RetryInfo], None] | None) -> PageExtractionResult:
        return extract_page(
            repo=repo,
            client=reader_client,
            run_id=run.id,
            run_month=run.month,
            page_no=page_no,
            image=image,
            settings=settings,
            on_retry=on_retry,
            sleep=sleep,
        )

    try:
        if repeat:
            reads = _stored_reads(repo, file.sha256, settings)
            assert reads is not None  # checked above; reads are never deleted
            out = _repeat(reads, run, file, done[page_no])
        else:
            out = _read_page(repo, run, page_no, user, read, now)
    except StateConflict:
        raise
    except (UpstreamFailed, PageUnreadable) as exc:
        _fail_extract(repo, notifier, run, page_no, exc, now, logged=not repeat)
        if isinstance(exc, PageUnreadable):
            raise ApiError(
                ErrorCode.CONTRACT_VIOLATION,
                422,
                str(exc),
                Stage.EXTRACT,
                {"problem": "PAGE_UNREADABLE", "page_no": page_no},
            ) from None
        raise
    except Exception as exc:
        _fail_extract(repo, notifier, run, page_no, exc, now, logged=not repeat)
        raise _unexpected(Stage.EXTRACT) from exc
    _finish_extracting(repo, run.id, settings)
    return out


def _repeat_past(
    repo: Repo,
    notifier: Notifier,
    run: Run,
    file: SourceFile,
    done: dict[int, bool],
    settings: Settings,
    now: Now,
) -> ExtractPageResult:
    """A repeat EP-09 on a run past extract: answered from the stored reads, status kept.
    A read missing under the current model and prompt is 409; an unexpected error sends one
    ops alert and is a generic 500, the run's status unchanged."""
    try:
        reads = _stored_reads(repo, file.sha256, settings)
        if reads is None:
            raise StateConflict(run)
        page_no = file.page_no or 0
        cached = done.get(page_no, True)  # both reads stored before this call: cached
        return _repeat(reads, run, file, cached)
    except StateConflict:
        raise
    except Exception as exc:
        stage_failed_alert(repo, notifier, run, Stage.EXTRACT, exc, file.page_no, now=now)
        raise _unexpected(Stage.EXTRACT) from exc


def _read_page(
    repo: Repo, run: Run, page_no: int, user: AllowedUser, read: Reader, now: Now
) -> ExtractPageResult:
    """One page read as a run step. Retries heard on the reader's worker threads are written
    as `RETRY` events here, on the request thread, after the read ends and before the step's
    `OK` or `FAILED`; so each RETRY event's `at` is the read's end time, not the retry's. A
    RETRY write that fails is logged by type name and never hides the read's outcome."""
    retries: list[RetryInfo] = []
    with stage_step(repo, run.id, Stage.EXTRACT, page_no=page_no, now=now) as step:
        try:
            result = read(retries.append)
        finally:
            for info in sorted(retries, key=lambda i: (i.variant.value, i.attempt)):
                detail = {"variant": info.variant.value, "attempt": info.attempt}
                try:
                    step.retry({**detail, "cause": info.cause})
                except Exception as exc:
                    log.error("RETRY event not written: %s", type(exc).__name__)
        out = _page_result(result, result.cached)
        step.base["cached"] = out.cached  # the OK event detail is {page_no, cached}
        audit(
            repo,
            user.email,
            AuditAction.PAGE_EXTRACTED,
            "page",
            result.page.page_sha256,
            run.id,
            after=out.model_dump(),
            channel=AuditChannel.WEB,
            now=now,
        )
        return out


def _fail_extract(
    repo: Repo,
    notifier: Notifier,
    run: Run,
    page_no: int,
    exc: Exception,
    now: Now,
    *,
    logged: bool,
) -> None:
    """FR-020, FM-06, constitution VIII: `FAILED` at `extract`, then one ops alert naming the
    page (its reason is built from the service, cause and counts, or the type name only).
    `logged=False` (the repeat path has no step) writes the extract `FAILED` event here."""
    if isinstance(exc, PageUnreadable):
        message = str(exc)
    elif isinstance(exc, UpstreamFailed):
        message = f"Page {page_no}: {exc}"
    else:
        message = f"Page {page_no}: {_internal_message(exc)}"
    if not logged:
        StepLog(repo, run.id, Stage.EXTRACT, now).write(
            RunEventStatus.FAILED, {"page_no": page_no, "error": message}
        )
    try:
        failed = repo.transition_run(
            run.id,
            RunStatus.FAILED,
            expected={RunStatus.EXTRACTING},
            failed_stage=Stage.EXTRACT,
            error_message=message[:ERROR_MESSAGE_LIMIT],
        )
    except StateConflict:  # a concurrent page call already failed (and alerted) the run
        return
    stage_failed_alert(repo, notifier, failed, Stage.EXTRACT, exc, page_no, now=now)


# --- EP-10 -----------------------------------------------------------------------------


class ReconcileResult(BaseModel):
    """EP-10 `ReconcileResponse` (data-model.md §2). A failing proof answers 200 with
    `status: FAILED` and `proof_status: FAIL` (EP-10 has no error code for it)."""

    status: RunStatus
    counts: dict[Outcome, int]
    proof_status: ProofStatus
    open_review_items: int


_RECONCILE_RETRY = frozenset({Stage.RECONCILE, Stage.PROOF})


def _register_input(repo: Repo, f: SourceFile, settings: Settings) -> RegisterPageInput:
    """One page's engine input: its stored reads merged by the Phase 2 merge (C23). A missing
    read is `NotFound`; a record that no longer validates raises `InvalidReadOutput`."""
    reads = _stored_reads(repo, f.sha256, settings)
    if reads is None:
        raise NotFound("extraction", f"page {f.page_no}")
    page = merge_reads(*reads, page_no=f.page_no or 0, page_sha256=f.sha256)
    return register_input(page)


def engine_inputs(repo: Repo, blobs: BlobStore, run: Run, settings: Settings) -> EngineInputs:
    """data-model.md §3 reconcile inputs: merged register pages from the stored reads, ERP and
    sheet bytes, the shipped mappings, and every review item of the run (engine contract:
    decisions of any status, so open extraction items reach C3). Missing inputs (a read, a
    blob, or not exactly one ERP or sheet file) are `NotFound`."""
    files = sorted(repo.list_source_files(run.id, Source.REGISTER), key=lambda f: f.page_no or 0)
    pages = [_register_input(repo, f, settings) for f in files]
    try:
        erp = _source_bytes(repo, blobs, run.id, Source.ERP)
        sheet = _source_bytes(repo, blobs, run.id, Source.SHEET)
    except ValueError as exc:  # `_source_bytes`: not exactly one confirmed file of the source
        raise NotFound("source_file", "ERP or SHEET") from exc
    return EngineInputs(
        run_id=run.id,
        month=run.month,
        register_pages=pages,
        erp_bytes=erp,
        sheet_bytes=sheet,
        mappings=load_mappings(),
        decisions=repo.list_review_items(run.id),
    )


def _fail_reconcile(repo: Repo, notifier: Notifier, run: Run, exc: Exception, now: Now) -> None:
    """An unexpected error before the engine ran: event `reconcile/FAILED`, `FAILED` at
    `reconcile` (a run already `FAILED` keeps its stage), one ops alert."""
    message = _internal_message(exc)
    StepLog(repo, run.id, Stage.RECONCILE, now).write(RunEventStatus.FAILED, {"error": message})
    current = run
    if run.status is not RunStatus.FAILED:
        try:
            current = repo.transition_run(
                run.id,
                RunStatus.FAILED,
                expected={run.status},
                failed_stage=Stage.RECONCILE,
                error_message=message,
            )
        except StateConflict:
            return
    stage_failed_alert(repo, notifier, current, Stage.RECONCILE, exc, now=now)


def reconcile_stage(
    repo: Repo,
    blobs: BlobStore,
    notifier: Notifier,
    run: Run,
    user: AllowedUser,
    fault: Fault | None,
    *,
    settings: Settings | None = None,
    now: Now = clock.now,
) -> ReconcileResult:
    """EP-10 (F-04 to F-07, FR-021, FR-022, FR-024; blueprint C25).

    Accepts `EXTRACTED`, `AWAITING_REVIEW`, `READY`, and `FAILED` at `reconcile` or `proof`
    (retry: `FAILED` → `EXTRACTED` first; OPS only this). Missing inputs → 500, state
    unchanged, one alert; any other input error → `FAILED(reconcile)`, one alert, generic 500.
    The Phase 1 `reconcile_run` does the rest (events, atomic writes, proof, review items,
    status moves, `RECONCILED` audit, `FAILED(proof)` on a failing proof, `FAILED(reconcile)`
    on its own errors, FM-08); this stage sends the ops alert on either and answers a failing
    proof with 200 (`status: FAILED`, `proof_status: FAIL`), an engine error with a generic
    500. `PROOF_DROP_ROW` reaches the engine; other faults are ignored here.
    """
    settings = settings or get_settings()
    retry = run.status is RunStatus.FAILED and run.failed_stage in _RECONCILE_RETRY
    if not retry and (user.role is Role.OPS or run.status not in RECONCILABLE):
        raise StateConflict(run)
    try:
        inputs = engine_inputs(repo, blobs, run, settings)
    except NotFound as exc:
        raise _inputs_missing(repo, notifier, run, Stage.RECONCILE, exc, now) from exc
    except Exception as exc:
        _fail_reconcile(repo, notifier, run, exc, now)
        raise _unexpected(Stage.RECONCILE) from exc
    if retry:
        run = repo.transition_run(run.id, RunStatus.EXTRACTED, expected={RunStatus.FAILED})
    faults = frozenset({fault}) if fault is Fault.PROOF_DROP_ROW else frozenset[Fault]()
    try:
        result = reconcile_run(repo, inputs, now=now, faults=faults)
    except StateConflict:
        raise
    except Exception as exc:
        failed = repo.get_run(run.id)
        if failed.status is RunStatus.FAILED and failed.failed_stage is Stage.RECONCILE:
            stage_failed_alert(repo, notifier, failed, Stage.RECONCILE, exc, now=now)
        raise _unexpected(Stage.RECONCILE) from exc
    current = repo.get_run(run.id)
    if result.proof.status is ProofStatus.FAIL:
        reason = (current.error_message or "proof failed")[:ERROR_MESSAGE_LIMIT]
        err = ApiError(ErrorCode.PROOF_NOT_PASSED, 409, reason, Stage.PROOF)
        stage_failed_alert(repo, notifier, current, Stage.PROOF, err, now=now)
    return ReconcileResult(
        status=current.status,
        counts=run_outcome_counts(repo, run.id),
        proof_status=result.proof.status,
        open_review_items=open_review_count(repo, run.id),
    )
