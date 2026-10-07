"""F-03 extraction service (contracts/extract-service.md). Pure functions make no I/O except
reading the shipped prompt and schema files."""

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import openai
from pydantic import ValidationError
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_chain,
    wait_fixed,
    wait_none,
)

from app.core import clock
from app.core.config import Settings
from app.core.db import Repo
from app.core.errors import InvalidReadOutput, PageUnreadable, UpstreamFailed
from app.domain.enums import (
    PRINTED_ROW_SLOTS,
    REVIEWABLE_REGISTER_FIELDS,
    CellStatus,
    ReadVariant,
    RegisterField,
    ReviewKind,
    ReviewReason,
)
from app.domain.ids import cache_file_name as cache_file_name  # re-export: contract §5
from app.domain.ids import (
    page_review_key,
    parse_month,
    register_row_id,
    review_item_key,
    row_review_key,
    stable_id,
)
from app.domain.models import (
    Cell,
    CellContext,
    Extraction,
    MergedCell,
    MergedPage,
    MergedRow,
    PageExtractionResult,
    PageLabelMismatchContext,
    PageRead,
    RegisterPageInput,
    ReviewItem,
    RowInOneReadContext,
)
from app.pipeline.model_client import ModelClient, RawRead

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def load_prompt(variant: ReadVariant, version: str) -> str:
    """Return the instructions for one read variant at one prompt version (F-03.R2).

    Files are named `read_{variant}_{version}.md`; a missing file raises FileNotFoundError.
    """
    path = PROMPTS_DIR / f"read_{variant.value.lower()}_{version}.md"
    return path.read_text(encoding="utf-8")


def load_read_schema(version: str) -> dict[str, Any]:
    """Return the strict read schema shipped for a prompt version (F-03.R4).

    Files are named `read_schema_{version}.json`; a missing file raises FileNotFoundError.
    """
    path = PROMPTS_DIR / f"read_schema_{version}.json"
    schema: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return schema


def parse_read(payload: object) -> PageRead:
    """Validate one model answer as a `PageRead` (data-model §1, FM-05).

    Accepts the parsed JSON object (or its JSON text). Validation is strict JSON mode, so no
    type is coerced (`"3"` is not a row index, `true` is not a count; enum strings are fine).
    Rows plus `blank_row_slots` must equal the 16 printed slots (PR-11, F-07.R3). A CORRECTED
    cell needs a non-blank current value and struck value. Any failure, including
    text that is not a JSON object, raises `InvalidReadOutput`; rows come back sorted by
    `row_index`.
    """
    if isinstance(payload, str | bytes):
        try:
            payload = json.loads(payload)
        except ValueError as exc:
            raise InvalidReadOutput(f"not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise InvalidReadOutput(f"expected a JSON object, got {type(payload).__name__}")
    try:
        text = json.dumps(payload, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise InvalidReadOutput(f"not a JSON object: {exc}") from exc
    try:
        read = PageRead.model_validate_json(text, strict=True)
    except ValidationError as exc:
        raise InvalidReadOutput(str(exc)) from exc
    if len(read.rows) + read.blank_row_slots != PRINTED_ROW_SLOTS:
        raise InvalidReadOutput(
            f"{len(read.rows)} rows plus blank_row_slots {read.blank_row_slots} is not "
            f"{PRINTED_ROW_SLOTS} printed slots (PR-11, F-07.R3)"
        )
    for row in read.rows:
        for field, cell in row.cells.items():
            if cell.status is CellStatus.CORRECTED and (
                same_key(cell.value) is None or same_key(cell.struck) is None
            ):
                raise InvalidReadOutput(
                    f"row {row.row_index} {field.value}: CORRECTED needs a non-blank value "
                    "and struck"
                )
    return read


# --- merge (data-model §3, FR-015 to FR-021) ---------------------------------------------


def same_key(value: str | None) -> str | None:
    """FR-015 comparison key: trimmed and case-folded; a blank string counts as null."""
    if value is None or not value.strip():
        return None
    return value.strip().casefold()


def _trimmed(value: str | None) -> str | None:
    return None if value is None or not value.strip() else value.strip()


def _union(*cells: Cell) -> list[str]:
    """FR-017: values (current value for CORRECTED) then candidates, read A first, trimmed,
    de-duplicated by the "same" rule."""
    seen: set[str] = set()
    out: list[str] = []
    for cell in cells:
        for raw in (cell.value, *cell.candidates):
            text, key = _trimmed(raw), same_key(raw)
            if text is not None and key is not None and key not in seen:
                seen.add(key)
                out.append(text)
    return out


def _join_notes(*notes: str | None) -> str | None:
    """Non-blank notes joined with " | ", skipping ones the "same" rule says repeat."""
    # Notes equal under the same-value rule (trimmed, case-folded) are de-duplicated.
    seen: set[str] = set()
    out: list[str] = []
    for note in notes:
        text, key = _trimmed(note), same_key(note)
        if text is not None and key is not None and key not in seen:
            seen.add(key)
            out.append(text)
    return " | ".join(out) if out else None


def _doubt(candidates: list[str], note: str | None) -> Cell:
    """A merged cell with no value: UNCLEAR with candidates, or ILLEGIBLE when none."""
    if candidates:
        return Cell(value=None, status=CellStatus.UNCLEAR, candidates=candidates, note=note)
    return Cell(value=None, status=CellStatus.ILLEGIBLE, note=note)


def _single_read_cell(cell: Cell) -> Cell:
    """FR-019 / Clarification 2: a cell only one read saw is never trusted."""
    value = _trimmed(cell.value)
    return _doubt([value] if value is not None else [], _trimmed(cell.note))


def _same_present(x: str | None, y: str | None) -> bool:
    """True when both strings are non-blank and the same under FR-015."""
    key = same_key(x)
    return key is not None and key == same_key(y)


def merge_cells(a: Cell | None, b: Cell | None) -> MergedCell:
    """Merge one cell pair by the precedence table (data-model §3), first match wins.

    `None` stands for a row the other read alone saw (FR-019): the cell becomes UNCLEAR with
    the read's value as the only candidate, or ILLEGIBLE, and carries no review reason.
    """
    if a is None:
        if b is None:
            raise ValueError("at least one read cell is required")
        return MergedCell(merged=_single_read_cell(b), read_a=None, read_b=b, review_reason=None)
    if b is None:
        return MergedCell(merged=_single_read_cell(a), read_a=a, read_b=None, review_reason=None)

    CLEAR, UNCLEAR = CellStatus.CLEAR, CellStatus.UNCLEAR
    CORRECTED, ILLEGIBLE = CellStatus.CORRECTED, CellStatus.ILLEGIBLE
    statuses = {a.status, b.status}
    note = _join_notes(a.note, b.note)
    reason: ReviewReason | None
    if ILLEGIBLE in statuses:  # 1
        merged = Cell(value=None, status=ILLEGIBLE, note=note)
        reason = ReviewReason.ILLEGIBLE
    elif (
        statuses == {CORRECTED}
        and _same_present(a.value, b.value)
        and _same_present(a.struck, b.struck)
    ):  # 2 (a blank key is never "same": it falls through to rule 3)
        merged = Cell(
            value=_trimmed(a.value), status=CORRECTED, struck=_trimmed(a.struck), note=note
        )
        reason = ReviewReason.CORRECTED
    elif CORRECTED in statuses:  # 3
        merged = _doubt(_union(a, b), note)
        reason = ReviewReason.READS_DISAGREE
    elif UNCLEAR in statuses:  # 4
        merged = _doubt(_union(a, b), note)
        reason = ReviewReason.UNCLEAR
    elif same_key(a.value) == same_key(b.value):  # 5: both CLEAR
        merged = Cell(value=_trimmed(a.value), status=CLEAR, note=note)
        reason = None
    else:  # 6: both CLEAR, different
        blank = [
            v for v, c in ((ReadVariant.A, a), (ReadVariant.B, b)) if same_key(c.value) is None
        ]
        if blank:
            note = _join_notes(note, f"read {blank[0].value} saw the cell blank")
        merged = _doubt(_union(a, b), note)
        reason = ReviewReason.READS_DISAGREE
    return MergedCell(merged=merged, read_a=a, read_b=b, review_reason=reason)


def _mean_edge(a: float | None, b: float | None) -> float | None:
    """FR-021: mean of both edges rounded half-up to 3 dp, else the one given, else None."""
    given = [Decimal(str(v)) for v in (a, b) if v is not None]
    if not given:
        return None
    mean = sum(given, Decimal(0)) / len(given)
    return float(mean.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))


def _merged_edges(read_a: PageRead, read_b: PageRead) -> tuple[float | None, float | None]:
    """FR-021 table edges; when both are known but top is not above bottom (edges from
    different reads, or a collapse after rounding), neither is trusted and both are None."""
    top = _mean_edge(read_a.table_top, read_b.table_top)
    bottom = _mean_edge(read_a.table_bottom, read_b.table_bottom)
    if top is not None and bottom is not None and top >= bottom:
        return None, None
    return top, bottom


def _agreed(a: str | None, b: str | None) -> str | None:
    """A header value both reads agree on (FR-015 "same"), as read A wrote it, else None."""
    return a if _same_present(a, b) else None


def merge_reads(
    read_a: PageRead, read_b: PageRead, *, page_no: int, page_sha256: str
) -> MergedPage:
    """Align both reads by row index (F-03.R8) and merge every cell (FR-016 to FR-021).

    Rows are sorted by row index and cells keep the fixed field order (FR-020). Month and page
    number as written are kept (read A's text) only when both reads give the same value under
    FR-015; when they differ or only one read gives one, neither is trusted and the value is
    None (the `PAGE_LABEL_MISMATCH` item keeps both reads' own values).
    """
    table_top, table_bottom = _merged_edges(read_a, read_b)
    rows_a = {r.row_index: r for r in read_a.rows}
    rows_b = {r.row_index: r for r in read_b.rows}
    rows: list[MergedRow] = []
    for index in sorted(rows_a.keys() | rows_b.keys()):
        row_a, row_b = rows_a.get(index), rows_b.get(index)
        cells = {
            field: merge_cells(
                None if row_a is None else row_a.cells[field],
                None if row_b is None else row_b.cells[field],
            )
            for field in RegisterField
        }
        rows.append(MergedRow(page_no=page_no, row_index=index, cells=cells))
    return MergedPage(
        page_no=page_no,
        page_sha256=page_sha256,
        month_written=_agreed(read_a.month_written, read_b.month_written),
        page_no_written=_agreed(read_a.page_no_written, read_b.page_no_written),
        table_top=table_top,
        table_bottom=table_bottom,
        blank_row_slots_a=read_a.blank_row_slots,
        blank_row_slots_b=read_b.blank_row_slots,
        row_count_a=len(read_a.rows),
        row_count_b=len(read_b.rows),
        rows=rows,
    )


def register_input(page: MergedPage) -> RegisterPageInput:
    """A merged page (E1) as the engine's register input (F-07.R3, blueprint C23). Field names
    differ: `blank_row_slots_a/b` become `blank_slots_a/b`; table edges and the page number as
    written are not engine inputs."""
    return RegisterPageInput(
        page_no=page.page_no,
        page_sha256=page.page_sha256,
        month_written=page.month_written,
        row_count_a=page.row_count_a,
        row_count_b=page.row_count_b,
        blank_slots_a=page.blank_row_slots_a,
        blank_slots_b=page.blank_row_slots_b,
        rows=page.rows,
    )


# --- page checks (FR-022, FR-023) --------------------------------------------------------

_MONTH_NAMES = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)


def _month_forms(run_month: str) -> frozenset[str]:
    """FR-022: the written forms that name `run_month` ("2026-09"), case-folded."""
    year, month = parse_month(run_month)
    name = _MONTH_NAMES[month - 1]
    names = {name[:3], name}
    if month == 9:
        names.add("sept")
    return frozenset(
        {f"{n} {year}" for n in names}
        | {f"{month:02}/{year}", f"{month}/{year}", f"{year}-{month:02}"}
    )


def page_label_ok(read: PageRead, *, page_no: int, run_month: str) -> bool:
    """True when the read's written page number equals the uploaded label and its written month
    names the run month (F-03.R9, FR-022). A missing value never matches."""
    written_page = (read.page_no_written or "").strip()
    page_ok = re.fullmatch(r"[0-9]+", written_page) is not None and int(written_page) == page_no
    month = " ".join((read.month_written or "").split()).casefold()
    return page_ok and month in _month_forms(run_month)


def check_readable(page: MergedPage) -> None:
    """Reject the page when more than half of its merged cells are ILLEGIBLE (FM-06, FR-023)."""
    cells = [c for r in page.rows for c in r.cells.values()]
    illegible = sum(1 for c in cells if c.merged.status is CellStatus.ILLEGIBLE)
    if illegible * 2 > len(cells):
        raise PageUnreadable(page.page_no, illegible, len(cells))


# --- review items (data-model §4, FR-019, FR-022, FR-024 to FR-026) ----------------------


PAGE_ITEM_FIELD = "page"
"""`ReviewItem.field` of the page label item; matches the `:page` suffix of `page_review_key`."""
ROW_ITEM_FIELD = "row"
"""`ReviewItem.field` of a row-in-one-read item; matches the `:row` suffix of `row_review_key`."""


def _single_read_variant(row: MergedRow) -> ReadVariant | None:
    """FR-019: the read that alone saw this row, or None when both reads saw it."""
    first = next(iter(row.cells.values()))
    if first.read_a is None:
        return ReadVariant.B
    if first.read_b is None:
        return ReadVariant.A
    return None


def _grn_of(row: MergedRow) -> int | None:
    """FR-026: the row's merged GRN when CLEAR and all digits, else None."""
    grn = row.cells[RegisterField.GRN_NO].merged
    if grn.status is CellStatus.CLEAR and grn.value is not None:
        if re.fullmatch(r"[0-9]+", grn.value.strip()):
            return int(grn.value)
    return None


def _page_item(
    page: MergedPage, read_a: PageRead, read_b: PageRead, run_id: UUID, run_month: str
) -> ReviewItem:
    key = page_review_key(page.page_no)
    return ReviewItem(
        id=stable_id(run_id, key),
        run_id=run_id,
        kind=ReviewKind.CELL,
        item_key=key,
        grn_no=None,
        source_row_id=None,
        field=PAGE_ITEM_FIELD,
        reason=ReviewReason.PAGE_LABEL_MISMATCH,
        read_a=None,
        read_b=None,
        context=PageLabelMismatchContext(
            page_no=page.page_no,
            run_month=run_month,
            month_written={
                ReadVariant.A: read_a.month_written,
                ReadVariant.B: read_b.month_written,
            },
            page_no_written={
                ReadVariant.A: read_a.page_no_written,
                ReadVariant.B: read_b.page_no_written,
            },
        ),
    )


def _row_items(row: MergedRow, run_id: UUID) -> list[ReviewItem]:
    row_id = register_row_id(row.page_no, row.row_index)
    grn_no = _grn_of(row)
    variant = _single_read_variant(row)
    if variant is not None:  # FR-019: one item for the row
        cells: dict[RegisterField, Cell] = {}
        for f, pair in row.cells.items():
            seen = pair.read_a if variant is ReadVariant.A else pair.read_b
            if seen is None:
                raise ValueError(f"row {row.row_index} {f.value}: read {variant.value} missing")
            cells[f] = seen
        key = row_review_key(row.page_no, row.row_index)
        return [
            ReviewItem(
                id=stable_id(run_id, key),
                run_id=run_id,
                kind=ReviewKind.CELL,
                item_key=key,
                grn_no=grn_no,
                source_row_id=row_id,
                field=ROW_ITEM_FIELD,
                reason=ReviewReason.ROW_IN_ONE_READ,
                read_a=None,
                read_b=None,
                context=RowInOneReadContext(
                    page_no=row.page_no, row_index=row.row_index, read=variant, cells=cells
                ),
            )
        ]
    items: list[ReviewItem] = []
    for field, cell in row.cells.items():
        if cell.review_reason is None or field not in REVIEWABLE_REGISTER_FIELDS:
            continue
        key = review_item_key(ReviewKind.CELL, row_id=row_id, field=field.value)
        items.append(
            ReviewItem(
                id=stable_id(run_id, key),
                run_id=run_id,
                kind=ReviewKind.CELL,
                item_key=key,
                grn_no=grn_no,
                source_row_id=row_id,
                field=field.value,
                reason=cell.review_reason,
                read_a=cell.read_a,
                read_b=cell.read_b,
                context=CellContext(
                    page_no=row.page_no,
                    row_index=row.row_index,
                    candidates=cell.merged.candidates,
                    struck=cell.merged.struck,
                    note=cell.merged.note,
                ),
            )
        )
    return items


def build_review_items(
    page: MergedPage, read_a: PageRead, read_b: PageRead, *, run_id: UUID, run_month: str
) -> list[ReviewItem]:
    """The CELL review items of one merged page (data-model §4), in a fixed order: the page
    label item first, then rows by row index, cells in field order. Ids are stable per key, so
    `Repo.upsert_review_items` keeps decisions already made (FR-025)."""
    items: list[ReviewItem] = []
    if not (
        page_label_ok(read_a, page_no=page.page_no, run_month=run_month)
        and page_label_ok(read_b, page_no=page.page_no, run_month=run_month)
    ):
        items.append(_page_item(page, read_a, read_b, run_id, run_month))
    for row in page.rows:
        items.extend(_row_items(row, run_id))
    return items


# --- service (contract §3, FR-001 to FR-014, FR-027 to FR-029) ---------------------------

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryInfo:
    """What the retry observer hears before each wait (FR-013); `attempt` is the next one."""

    page_no: int
    variant: ReadVariant
    attempt: int
    cause: str


def image_mime(image: bytes) -> str:
    """MIME from magic bytes (research R3); anything else is refused before any call."""
    if image.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    raise ValueError("page image is not a JPEG or PNG")


@dataclass
class _ReadOutcome:
    read: PageRead | None = None
    raw: RawRead | None = None
    attempts: int = 0
    error: BaseException | None = None


def _read_with_retries(
    client: ModelClient,
    *,
    image: bytes,
    mime: str,
    variant: ReadVariant,
    instructions: str,
    schema: dict[str, Any],
    page_no: int,
    attempts: int,
    waits: tuple[int, ...],
    on_retry: Callable[[RetryInfo], None] | None,
    sleep: Callable[[float], None],
) -> _ReadOutcome:
    """One read with bounded retries (contract §4, research R5): an attempt is a model call
    plus validation; up to `attempts` (settings `EXTRACT_ATTEMPTS`, default 3) and retryable
    failures wait `waits` (`EXTRACT_BACKOFF_S`, default 2 s then 8 s; the last repeats) via
    `sleep`, reporting each retry to `on_retry` first. `on_retry` runs on this read's worker
    thread; an exception it raises is logged and ignored, never aborting the read. Never
    raises: the last error is returned in the outcome."""
    outcome = _ReadOutcome()

    def attempt() -> tuple[PageRead, RawRead]:
        outcome.attempts += 1
        answer = client.read_page(
            image=image, mime=mime, variant=variant, instructions=instructions, schema=schema
        )
        return parse_read(answer.payload), answer

    def before_sleep(state: RetryCallState) -> None:
        error = state.outcome.exception() if state.outcome is not None else None
        cause = retry_cause(error) or "unknown"
        info = RetryInfo(page_no, variant, state.attempt_number + 1, cause)
        logger.warning(
            "extract page=%d read=%s attempt %d failed (%s); retrying",
            page_no,
            variant.value,
            state.attempt_number,
            cause,
        )
        if on_retry is not None:
            try:
                on_retry(info)
            except Exception:  # an observer bug must not cost the read
                logger.exception(
                    "extract page=%d read=%s on_retry observer failed; ignored",
                    page_no,
                    variant.value,
                )

    retrying = Retrying(
        stop=stop_after_attempt(attempts),
        wait=wait_chain(*(wait_fixed(s) for s in waits)) if waits else wait_none(),
        retry=retry_if_exception(lambda e: retry_cause(e) is not None),
        before_sleep=before_sleep,
        sleep=sleep,
        reraise=True,
    )
    try:
        outcome.read, outcome.raw = retrying(attempt)
    except Exception as exc:
        outcome.error = exc
    return outcome


def retry_cause(error: BaseException | None) -> str | None:
    """The contract §4 cause of a retryable failure, or None when it must not be retried."""
    if isinstance(error, openai.APITimeoutError):  # before its base, APIConnectionError
        return "timeout"
    if isinstance(error, openai.APIConnectionError):
        return "connection error"
    if isinstance(error, openai.RateLimitError):
        return "rate limited"
    if isinstance(error, openai.InternalServerError):
        return "server error"
    if isinstance(error, openai.APIStatusError):
        if error.status_code == 429:
            return "rate limited"
        if error.status_code >= 500:
            return "server error"
    if isinstance(error, InvalidReadOutput):
        return "invalid output"
    return None


def _final_error(error: BaseException, attempts: int, page_no: int) -> BaseException:
    """Contract §4 final detail. Any other `openai.APIError` (e.g. a response that fails the
    SDK's own validation) is upstream trouble too (FR-012); non-openai errors (bugs, e.g. a
    replay cache miss) are returned unchanged."""
    cause = retry_cause(error)
    if cause is not None:
        detail = f"{cause} after {attempts} attempts"
    elif isinstance(error, openai.APIStatusError):
        detail = f"rejected: HTTP {error.status_code}"
    elif isinstance(error, openai.APIError):
        detail = f"rejected: {type(error).__name__}"
    else:
        return error
    return UpstreamFailed("openai", detail, stage="extract", page_no=page_no, attempts=attempts)


def _cached_read(repo: Repo, sha: str, variant: ReadVariant, settings: Settings) -> PageRead | None:
    record = repo.get_extraction(sha, variant, settings.EXTRACT_MODEL, settings.PROMPT_VERSION)
    if record is None:
        return None
    try:
        return parse_read(record.result.model_dump(mode="json"))
    except InvalidReadOutput as exc:
        raise RuntimeError(
            f"cache record invalid: {sha[:12]} variant {variant.value} "
            f"{settings.EXTRACT_MODEL} {settings.PROMPT_VERSION}"
        ) from exc


def _store(
    repo: Repo, sha: str, variant: ReadVariant, settings: Settings, outcome: _ReadOutcome
) -> PageRead:
    """FR-007: store one successful read at once. On a key conflict the first record wins and
    its read is the one merged, so the page stays re-derivable from the cache (FR-028)."""
    assert outcome.read is not None and outcome.raw is not None
    record, _created = repo.put_extraction(
        Extraction(
            id=uuid4(),
            page_sha256=sha,
            read_variant=variant,
            model=settings.EXTRACT_MODEL,
            prompt_version=settings.PROMPT_VERSION,
            result=outcome.read,
            input_tokens=outcome.raw.input_tokens,
            output_tokens=outcome.raw.output_tokens,
            latency_ms=outcome.raw.latency_ms,
            created_at=clock.now(),
        )
    )
    return record.result


def _disagreements(page: MergedPage) -> int:
    """Data-model §5: READS_DISAGREE cells (any field) plus rows seen by one read only."""
    count = 0
    for row in page.rows:
        # A row one read alone saw counts once, never per cell: its merged cells carry no
        # review reason (FR-019), and it becomes one ROW_IN_ONE_READ item.
        if _single_read_variant(row) is not None:
            count += 1
            continue
        count += sum(
            1 for c in row.cells.values() if c.review_reason is ReviewReason.READS_DISAGREE
        )
    return count


def _obtain_reads(
    repo: Repo,
    client: ModelClient,
    *,
    sha: str,
    image: bytes,
    mime: str,
    page_no: int,
    settings: Settings,
    on_retry: Callable[[RetryInfo], None] | None,
    sleep: Callable[[float], None],
) -> tuple[dict[ReadVariant, PageRead], int]:
    """Contract §3 steps 2 to 5: both reads (cached, else read in parallel) and the model
    calls made. Each successful read is stored as soon as it completes (FR-007), on this
    thread, so the repo is never used from a worker. When any read or store fails, every
    other read is still awaited and each success stored before the first failure in variant
    order (A before B) is raised; a store error is raised unchanged."""
    reads: dict[ReadVariant, PageRead] = {}
    for v in ReadVariant:
        cached = _cached_read(repo, sha, v, settings)
        if cached is not None:
            reads[v] = cached
    missing = [v for v in ReadVariant if v not in reads]
    if not missing:
        return reads, 0
    schema = load_read_schema(settings.PROMPT_VERSION)
    prompts = {v: load_prompt(v, settings.PROMPT_VERSION) for v in missing}
    calls_made = 0
    errors: dict[ReadVariant, BaseException] = {}
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="extract-read") as pool:
        futures: dict[Future[_ReadOutcome], ReadVariant] = {
            pool.submit(
                _read_with_retries,
                client,
                image=image,
                mime=mime,
                variant=v,
                instructions=prompts[v],
                schema=schema,
                page_no=page_no,
                attempts=settings.EXTRACT_ATTEMPTS,
                waits=settings.EXTRACT_BACKOFF_S,
                on_retry=on_retry,
                sleep=sleep,
            ): v
            for v in missing
        }
        for future in as_completed(futures):  # store each success at once (US3 sc. 5)
            variant, outcome = futures[future], future.result()
            calls_made += outcome.attempts
            if outcome.error is None:
                try:
                    reads[variant] = _store(repo, sha, variant, settings, outcome)
                except Exception as exc:  # keep storing the other read (FR-007)
                    errors[variant] = exc
            else:
                errors[variant] = _final_error(outcome.error, outcome.attempts, page_no)
    if errors:
        failure = next(errors[v] for v in ReadVariant if v in errors)  # deterministic: A first
        logger.warning(
            "extract page=%d sha=%s failed: %s",
            page_no,
            sha[:12],
            failure.detail if isinstance(failure, UpstreamFailed) else type(failure).__name__,
        )
        raise failure
    return reads, calls_made


def extract_page(
    *,
    repo: Repo,
    client: ModelClient,
    run_id: UUID,
    run_month: str,
    page_no: int,
    image: bytes,
    settings: Settings,
    on_retry: Callable[[RetryInfo], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> PageExtractionResult:
    """Read one register page twice (cached per variant), merge, check and queue review items
    (contract §3). Never changes run status, run events or audit (FR-029); `on_retry` runs on
    the read's worker thread, and an exception it raises is logged and ignored. Never logs the
    API key or the image.

    Raises:
        ValueError: the image is not a JPEG or PNG (before any lookup or call).
        RuntimeError: a cached record no longer validates ("cache record invalid").
        UpstreamFailed: a read could not be obtained (service "openai", stage "extract").
        PageUnreadable: more than half the merged cells are illegible (FM-06); nothing queued.
    """
    sha = hashlib.sha256(image).hexdigest()  # 1
    mime = image_mime(image)
    reads, calls_made = _obtain_reads(  # 2 to 5
        repo,
        client,
        sha=sha,
        image=image,
        mime=mime,
        page_no=page_no,
        settings=settings,
        on_retry=on_retry,
        sleep=sleep,
    )
    read_a, read_b = reads[ReadVariant.A], reads[ReadVariant.B]
    page = merge_reads(read_a, read_b, page_no=page_no, page_sha256=sha)  # 6
    check_readable(page)  # 7
    items = build_review_items(page, read_a, read_b, run_id=run_id, run_month=run_month)  # 8
    repo.upsert_review_items(run_id, items)
    result = PageExtractionResult(  # 9
        page_no=page_no,
        cached=calls_made == 0,
        page=page,
        review_items=items,
        review_cells=len(items),
        disagreements=_disagreements(page),
        calls_made=calls_made,
    )
    logger.info(
        "extract page=%d sha=%s cached=%s calls=%d review_cells=%d disagreements=%d",
        page_no,
        sha[:12],
        result.cached,
        calls_made,
        result.review_cells,
        result.disagreements,
    )
    return result


# --- pack cache helpers (contract §5, research R10, FR-030, FR-031) ------------------------


def load_cached_extractions(repo: Repo, cache_dir: Path) -> int:
    """Load every committed read in `cache_dir` (`*.json`, each a full `Extraction`) into
    `repo`; returns how many were newly stored, so a second load returns 0 (idempotent).

    Raises:
        FileNotFoundError: `cache_dir` does not exist.
        ValueError: a file is not a valid `Extraction`, or its name is not the
            `cache_file_name` of its own record.
    """
    cache_dir = Path(cache_dir)
    if not cache_dir.is_dir():
        raise FileNotFoundError(f"cache directory not found: {cache_dir}")
    records: list[Extraction] = []
    for path in sorted(cache_dir.glob("*.json")):  # validate all before storing any
        try:
            record = Extraction.model_validate_json(path.read_text(encoding="utf-8"))
        except ValidationError as exc:
            raise ValueError(f"cached read {path.name} is not a valid extraction") from exc
        expected = cache_file_name(
            record.page_sha256, record.read_variant, record.model, record.prompt_version
        )
        if path.name != expected:
            raise ValueError(f"cached read {path.name} does not match its record ({expected})")
        records.append(record)
    return sum(1 for record in records if repo.put_extraction(record)[1])
