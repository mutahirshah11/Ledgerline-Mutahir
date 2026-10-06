"""F-03 extraction service (contracts/extract-service.md). Pure functions make no I/O except
reading the shipped prompt and schema files."""

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError

from app.core import clock
from app.core.config import Settings
from app.core.db import Repo
from app.core.errors import InvalidReadOutput, PageUnreadable, UpstreamFailed
from app.domain.enums import (
    REVIEWABLE_REGISTER_FIELDS,
    CellStatus,
    ReadVariant,
    RegisterField,
    ReviewKind,
    ReviewReason,
)
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

    Accepts the parsed JSON object (or its JSON text). Any failure, including text that is not
    a JSON object, raises `InvalidReadOutput`; rows come back sorted by `row_index`.
    """
    if isinstance(payload, str | bytes):
        try:
            payload = json.loads(payload)
        except ValueError as exc:
            raise InvalidReadOutput(f"not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise InvalidReadOutput(f"expected a JSON object, got {type(payload).__name__}")
    try:
        return PageRead.model_validate(payload)
    except ValidationError as exc:
        raise InvalidReadOutput(str(exc)) from exc


# --- merge (data-model §3, FR-015 to FR-021) ---------------------------------------------


def _same_key(value: str | None) -> str | None:
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
            text, key = _trimmed(raw), _same_key(raw)
            if text is not None and key is not None and key not in seen:
                seen.add(key)
                out.append(text)
    return out


def _join_notes(*notes: str | None) -> str | None:
    """Non-blank notes joined with " | ", skipping ones the "same" rule says repeat."""
    seen: set[str] = set()
    out: list[str] = []
    for note in notes:
        text, key = _trimmed(note), _same_key(note)
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


def merge_cells(a: Cell | None, b: Cell | None) -> MergedCell:
    """Merge one cell pair by the precedence table (data-model §3), first match wins.

    `None` stands for a row the other read alone saw (FR-019): the cell becomes UNCLEAR with
    the read's value as the only candidate, or ILLEGIBLE, and carries no review reason.
    """
    if a is None and b is None:
        raise ValueError("at least one read cell is required")
    if a is None or b is None:
        only = b if a is None else a
        assert only is not None
        return MergedCell(merged=_single_read_cell(only), read_a=a, read_b=b, review_reason=None)

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
        and _same_key(a.value) == _same_key(b.value)
        and _same_key(a.struck) == _same_key(b.struck)
    ):  # 2
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
    elif _same_key(a.value) == _same_key(b.value):  # 5: both CLEAR
        merged = Cell(value=_trimmed(a.value), status=CLEAR, note=note)
        reason = None
    else:  # 6: both CLEAR, different
        blank = [
            v for v, c in ((ReadVariant.A, a), (ReadVariant.B, b)) if _same_key(c.value) is None
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


def merge_reads(
    read_a: PageRead, read_b: PageRead, *, page_no: int, page_sha256: str
) -> MergedPage:
    """Align both reads by row index (F-03.R8) and merge every cell (FR-016 to FR-021).

    Rows are sorted by row index and cells keep the fixed field order (FR-020). Month and page
    number as written are read A's, or read B's when A has none.
    """
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
        month_written=read_a.month_written
        if read_a.month_written is not None
        else read_b.month_written,
        page_no_written=read_a.page_no_written
        if read_a.page_no_written is not None
        else read_b.page_no_written,
        table_top=_mean_edge(read_a.table_top, read_b.table_top),
        table_bottom=_mean_edge(read_a.table_bottom, read_b.table_bottom),
        blank_row_slots_a=read_a.blank_row_slots,
        blank_row_slots_b=read_b.blank_row_slots,
        row_count_a=len(read_a.rows),
        row_count_b=len(read_b.rows),
        rows=rows,
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
        field="page",
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
    first = next(iter(row.cells.values()))
    if first.read_a is None or first.read_b is None:  # FR-019: one item for the row
        variant = ReadVariant.B if first.read_a is None else ReadVariant.A
        side = "read_b" if variant is ReadVariant.B else "read_a"
        cells = {f: getattr(c, side) for f, c in row.cells.items()}
        key = row_review_key(row.page_no, row.row_index)
        return [
            ReviewItem(
                id=stable_id(run_id, key),
                run_id=run_id,
                kind=ReviewKind.CELL,
                item_key=key,
                grn_no=grn_no,
                source_row_id=row_id,
                field="row",
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
    on_retry: Callable[[RetryInfo], None] | None,
    sleep: Callable[[float], None],
) -> _ReadOutcome:
    """One read: call the model and validate the answer (single attempt)."""
    outcome = _ReadOutcome()
    try:
        outcome.attempts += 1
        answer = client.read_page(
            image=image, mime=mime, variant=variant, instructions=instructions, schema=schema
        )
        outcome.read, outcome.raw = parse_read(answer.payload), answer
    except Exception as exc:
        outcome.error = exc
    return outcome


def _upstream_failed(error: BaseException, attempts: int, page_no: int) -> UpstreamFailed:
    """Contract §4 cause table."""
    if isinstance(error, InvalidReadOutput):
        detail = f"invalid output after {attempts} attempts"
    else:
        detail = f"{type(error).__name__} after {attempts} attempts"
    return UpstreamFailed("openai", detail, stage="extract", page_no=page_no)


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
        first = next(iter(row.cells.values()))
        if first.read_a is None or first.read_b is None:
            count += 1
            continue
        count += sum(
            1 for c in row.cells.values() if c.review_reason is ReviewReason.READS_DISAGREE
        )
    return count


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
    the read's worker thread. Never logs the API key or the image.

    Raises:
        ValueError: the image is not a JPEG or PNG (before any lookup or call).
        RuntimeError: a cached record no longer validates ("cache record invalid").
        UpstreamFailed: a read could not be obtained (service "openai", stage "extract").
        PageUnreadable: more than half the merged cells are illegible (FM-06); nothing queued.
    """
    sha = hashlib.sha256(image).hexdigest()
    mime = image_mime(image)
    reads: dict[ReadVariant, PageRead | None] = {
        v: _cached_read(repo, sha, v, settings) for v in ReadVariant
    }
    missing = [v for v, r in reads.items() if r is None]
    calls_made = 0
    if missing:
        schema = load_read_schema(settings.PROMPT_VERSION)
        prompts = {v: load_prompt(v, settings.PROMPT_VERSION) for v in missing}
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="extract-read") as pool:
            futures: dict[ReadVariant, Future[_ReadOutcome]] = {
                v: pool.submit(
                    _read_with_retries,
                    client,
                    image=image,
                    mime=mime,
                    variant=v,
                    instructions=prompts[v],
                    schema=schema,
                    page_no=page_no,
                    on_retry=on_retry,
                    sleep=sleep,
                )
                for v in missing
            }
            outcomes = {v: f.result() for v, f in futures.items()}
        failure: UpstreamFailed | None = None
        for variant, outcome in outcomes.items():  # store every success first (US3 sc. 5)
            calls_made += outcome.attempts
            if outcome.error is None:
                reads[variant] = _store(repo, sha, variant, settings, outcome)
            elif failure is None:
                failure = _upstream_failed(outcome.error, outcome.attempts, page_no)
        if failure is not None:
            logger.warning("extract page=%d sha=%s failed: %s", page_no, sha[:12], failure.detail)
            raise failure

    read_a, read_b = reads[ReadVariant.A], reads[ReadVariant.B]
    assert read_a is not None and read_b is not None
    page = merge_reads(read_a, read_b, page_no=page_no, page_sha256=sha)
    check_readable(page)
    items = build_review_items(page, read_a, read_b, run_id=run_id, run_month=run_month)
    repo.upsert_review_items(run_id, items)
    result = PageExtractionResult(
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
