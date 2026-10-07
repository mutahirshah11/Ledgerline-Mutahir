"""F-02 input validation: pure contract checks over file bytes (Section 7, Section 14).

Reads only; never changes data (F-02.R5). Every problem from every file is returned, in source
order REGISTER, ERP, SHEET (`order.SOURCE_ORDER`) and file order within a source (F-02.R4).
Column names, headers and the worksheet name come from `normalise`, so F-02 checks exactly what
F-04 later reads.
"""

import csv
import io
import re
import struct
from collections.abc import Callable, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import NamedTuple

import openpyxl

from app.domain.enums import ProblemCode, Source
from app.domain.models import DomainModel
from app.pipeline.normalise import ERP_COLUMNS as ERP_COLUMNS
from app.pipeline.normalise import SHEET_HEADERS as SHEET_HEADERS
from app.pipeline.normalise import sheet_name as sheet_name
from app.pipeline.order import SOURCE_ORDER

MIN_IMAGE_PX = 800
MAX_PAGES = 10
QTY_MAX_EXPONENT = 15
"""As normalise (F-04.R7): a qty of 10^15 or more is unreadable, so F-02 refuses it too."""

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Baseline, extended and progressive frames only (research R6); other SOF frames are refused.
# C4 (DHT), C8 (JPG) and CC (DAC) are ordinary segments, not frames.
_JPEG_SOF = frozenset({0xC0, 0xC1, 0xC2})
_JPEG_OTHER_SOF = frozenset(range(0xC3, 0xD0)) - {0xC4, 0xC8, 0xCC}
_JPEG_STANDALONE = frozenset({0x01, *range(0xD0, 0xD8)})
_UNCLOSED_QUOTE = ("unexpected end of data", "field larger than field limit")


class ProblemLocation(DomainModel):
    """Where a problem is: ERP data `line` (1 = first data row) and `column`; sheet `row`,
    `column` (header) and `worksheet`; register `page_no` (data-model.md, Validation)."""

    line: int | None = None
    row: int | None = None
    column: str | None = None
    worksheet: str | None = None
    page_no: int | None = None


class ValidationProblem(DomainModel):
    """One broken contract rule, in plain language for SCR-03 (F-02.R4)."""

    source: Source
    code: ProblemCode
    message: str
    location: ProblemLocation | None = None


class RegisterFile(NamedTuple):
    """One uploaded register page: its page number, original file name and bytes."""

    page_no: int
    filename: str
    data: bytes


def _problem(
    source: Source, code: ProblemCode, message: str, **location: int | str
) -> ValidationProblem:
    loc = ProblemLocation.model_validate(location) if location else None
    return ValidationProblem(source=source, code=code, message=message, location=loc)


# --- R1 ERP -------------------------------------------------------------------------------


def _is_decimal(value: str) -> bool:
    """What normalise reads as a qty (F-04.R7): `Decimal` text as written (padding allowed),
    no `_` numerals, finite, below 10^15. Blank fails: F-02.R1 wants a decimal on every row."""
    if not value.strip() or "_" in value:
        return False
    try:
        qty = Decimal(value)
    except InvalidOperation:
        return False
    return qty.is_finite() and qty.adjusted() < QTY_MAX_EXPONENT


def _is_iso_date(value: str) -> bool:
    if not _ISO_DATE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _erp_header_problems(header: list[str]) -> list[ValidationProblem]:
    """One MISSING_COLUMN per required column absent or repeated, in column order."""
    problems = []
    for c in ERP_COLUMNS:
        count = header.count(c)
        if count == 1:
            continue
        times = "twice" if count == 2 else f"{count} times"
        msg = f"ERP: column {c} is missing" if count == 0 else f"ERP: column {c} appears {times}"
        problems.append(_problem(Source.ERP, ProblemCode.MISSING_COLUMN, msg, column=c))
    return problems


def check_erp(data: bytes) -> list[ValidationProblem]:
    """F-02.R1: UTF-8 (BOM allowed), quoted CSV, the ten columns once each, ≥ 1 data row, every
    row as wide as the header, decimal `qty` and ISO `grn_date` on every data row.

    Lines are numbered as normalise's `ERP-{line:04}` ids: line 1 = first data row and only
    empty lines are skipped (rows of bare commas count). A CSV that won't parse is one ENCODING.
    """
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        msg = f"ERP: file is not UTF-8 text (bad byte at position {exc.start})"
        return [_problem(Source.ERP, ProblemCode.ENCODING, msg)]
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    rows: list[list[str]] = []
    try:
        header = next(reader, [])
        rows.extend(r for r in reader if r != [])
    except csv.Error as exc:
        reason = "unclosed quote" if str(exc).startswith(_UNCLOSED_QUOTE) else "bad quoting"
        msg = f"ERP: file is not valid CSV ({reason} near line {len(rows) + 1})"
        return [_problem(Source.ERP, ProblemCode.ENCODING, msg)]
    problems = _erp_header_problems(header)
    if not rows:
        problems.append(_problem(Source.ERP, ProblemCode.NO_DATA_ROWS, "ERP: no data rows"))
    checks: tuple[tuple[str, ProblemCode, Callable[[str], bool], str], ...] = (
        ("qty", ProblemCode.BAD_NUMBER, _is_decimal, "is not a number"),
        ("grn_date", ProblemCode.BAD_DATE, _is_iso_date, "is not a date YYYY-MM-DD"),
    )
    index = {name: header.index(name) for name, *_ in checks if name in header}
    for line, row in enumerate(rows, start=1):
        if len(row) != len(header):
            msg = f"ERP line {line}: expected {len(header)} values, found {len(row)}"
            problems.append(_problem(Source.ERP, ProblemCode.MISSING_COLUMN, msg, line=line))
            continue
        for name, code, ok, what in checks:
            if name in index and not ok(value := row[index[name]]):
                msg = f'ERP line {line}: {name} "{value}" {what}'
                problems.append(_problem(Source.ERP, code, msg, line=line, column=name))
    return problems


# --- R2 sheet -----------------------------------------------------------------------------


def check_sheet(month: str, data: bytes) -> list[ValidationProblem]:
    """F-02.R2: openpyxl read-only with values; worksheet `Mon-YYYY` for the run month (`month`
    already validated as `YYYY-MM`); the eight headers in row 1, exact text; ≥ 1 data row.
    Cell values are not parsed (F-02.AC6: F-04's job). Damage found while opening the workbook
    or reading its rows is one ENCODING problem, as in `normalise.read_sheet`.
    """
    name = sheet_name(month)
    found: list[str] = []
    header: tuple[object, ...] = ()
    has_data = False
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        try:
            found = list(wb.sheetnames)
            if name in found:
                rows = wb[name].iter_rows(values_only=True)
                header = next(rows, ())
                has_data = any(any(v is not None and str(v).strip() for v in r) for r in rows)
        finally:
            wb.close()
    except Exception:  # BadZipFile, KeyError, ParseError, ...: varies by damage
        msg = "Store sheet: file is not a readable .xlsx workbook"
        return [_problem(Source.SHEET, ProblemCode.ENCODING, msg)]
    if name not in found:
        msg = f"Store sheet: no worksheet named {name} (found: {', '.join(found) or 'none'})"
        return [_problem(Source.SHEET, ProblemCode.MISSING_WORKSHEET, msg, worksheet=name)]
    problems = [
        _problem(
            Source.SHEET,
            ProblemCode.MISSING_HEADER,
            f"Store sheet: header {h} is missing from row 1",
            row=1,
            column=h,
            worksheet=name,
        )
        for h in SHEET_HEADERS
        if h not in header
    ]
    if not has_data:
        msg = f"Store sheet: worksheet {name} has no data rows"
        problems.append(_problem(Source.SHEET, ProblemCode.NO_DATA_ROWS, msg, worksheet=name))
    return problems


# --- R3 register --------------------------------------------------------------------------


def _png_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or not data.startswith(_PNG_SIGNATURE) or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    if not data.startswith(b"\xff\xd8"):
        return None
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker in _JPEG_STANDALONE:
            i += 2
            continue
        if marker in (0xD9, 0xDA) or marker in _JPEG_OTHER_SOF:
            return None  # end of image, scan data before any frame, or an unsupported frame
        (length,) = struct.unpack(">H", data[i + 2 : i + 4])
        if marker in _JPEG_SOF:
            if i + 9 > len(data):
                return None
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return width, height
        i += 2 + length
    return None


def image_size(data: bytes) -> tuple[int, int] | None:
    """Width and height from a PNG IHDR or a JPEG SOF0/SOF1/SOF2 header, standard library only
    (research R6); None when the bytes are neither or the header can't be parsed."""
    size = _png_size(data) or _jpeg_size(data)
    if size is None or 0 in size:
        return None
    return size


def check_register(files: Sequence[RegisterFile]) -> list[ValidationProblem]:
    """F-02.R3: at most ten pages; each opens as a PNG or JPEG at least 800 × 800 px.
    Problems follow the order of `files` (callers pass them by page number)."""
    problems: list[ValidationProblem] = []
    if len(files) > MAX_PAGES:
        msg = f"Register: {len(files)} pages uploaded; at most {MAX_PAGES} allowed"
        problems.append(_problem(Source.REGISTER, ProblemCode.TOO_MANY_PAGES, msg))
    for f in files:
        where = f"Register page {f.page_no} ({f.filename})"
        size = image_size(f.data)
        if size is None:
            msg = f"{where}: not a readable JPEG or PNG image"
            problems.append(
                _problem(Source.REGISTER, ProblemCode.IMAGE_UNREADABLE, msg, page_no=f.page_no)
            )
        elif min(size) < MIN_IMAGE_PX:
            msg = (
                f"{where}: image is {size[0]} x {size[1]} px; "
                f"at least {MIN_IMAGE_PX} x {MIN_IMAGE_PX} needed"
            )
            problems.append(
                _problem(Source.REGISTER, ProblemCode.IMAGE_TOO_SMALL, msg, page_no=f.page_no)
            )
    return problems


# --- R4 all files -------------------------------------------------------------------------


def validate_files(
    month: str, erp_bytes: bytes, sheet_bytes: bytes, register_files: Sequence[RegisterFile]
) -> list[ValidationProblem]:
    """Every problem across all files in `SOURCE_ORDER` (REGISTER, ERP, SHEET) (F-02.R4);
    [] = pass. `month` is the run's already validated `YYYY-MM`."""
    checks: dict[Source, Callable[[], list[ValidationProblem]]] = {
        Source.REGISTER: lambda: check_register(register_files),
        Source.ERP: lambda: check_erp(erp_bytes),
        Source.SHEET: lambda: check_sheet(month, sheet_bytes),
    }
    return [p for source in SOURCE_ORDER for p in checks[source]()]
