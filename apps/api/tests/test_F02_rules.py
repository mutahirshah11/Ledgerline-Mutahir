"""F-02 contract checks, pure functions (T035-T038; research R6). API-level AC tests: T034."""

import io
import struct
import zlib
from pathlib import Path

import openpyxl
import pytest

from app.domain.enums import ProblemCode, Source
from app.pipeline.validate import (
    ProblemLocation,
    RegisterFile,
    check_erp,
    check_register,
    check_sheet,
    sheet_name,
    validate_files,
)
from tests.support.pack import ERP_FILE, PACK_MONTH, PAGES, SHEET_FILE

ERP_HEADER = (
    "grn_no,grn_date,supplier_code,supplier_name,item_desc,lot_no,qty,uom,entered_by,entered_at"
)
SHEET_HEADERS = ["Date", "GRN", "Supplier", "Material", "Lot", "Qty Received", "UOM", "Remarks"]


@pytest.fixture(scope="module")
def erp_bytes(pack_dir: Path) -> bytes:
    return (pack_dir / ERP_FILE).read_bytes()


@pytest.fixture(scope="module")
def sheet_bytes(pack_dir: Path) -> bytes:
    return (pack_dir / SHEET_FILE).read_bytes()


@pytest.fixture(scope="module")
def register(pack_dir: Path) -> list[RegisterFile]:
    return [
        RegisterFile(
            p, f"t4_register_page{p}.jpg", (pack_dir / f"t4_register_page{p}.jpg").read_bytes()
        )
        for p in PAGES
    ]


def _erp(*rows: str) -> bytes:
    return "\r\n".join([ERP_HEADER, *rows, ""]).encode()


GOOD_ROW = "GRN-4401,2026-09-01,S-101,QASR,CARTONS,L2609-001,2000,PCS,store.clerk,2026-09-01 18:08"


def _xlsx(title: str = "Sep-2026", headers: list[str] | None = None, rows: int = 1) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = title
    ws.append(headers if headers is not None else SHEET_HEADERS)
    for _ in range(rows):
        ws.append(["9/14/2026", 4401, "Qasr", "Cartons", "L2609-001", 2000, "pcs", None])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _png(width: int, height: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr
    return b"\x89PNG\r\n\x1a\n" + chunk + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))


def _jpeg(width: int, height: int, sof: int = 0xC0) -> bytes:
    app0 = (
        b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    )
    sof_seg = (
        b"\xff" + bytes([sof]) + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    )
    return b"\xff\xd8" + app0 + sof_seg + b"\xff\xd9"


# --- R1 ERP -----------------------------------------------------------------------------


def test_F02_R1_pack_erp_passes(erp_bytes: bytes) -> None:
    assert check_erp(erp_bytes) == []


def test_F02_R1_bom_accepted() -> None:
    assert check_erp(b"\xef\xbb\xbf" + _erp(GOOD_ROW)) == []


def test_F02_R1_quoted_values_accepted() -> None:
    row = 'GRN-4401,2026-09-01,S-101,"QASR, LTD","GREY FABRIC 63""",L1,"1097.28",M,a,b'
    assert check_erp(_erp(row)) == []


def test_F02_R1_not_utf8_encoding() -> None:
    problems = check_erp(_erp(GOOD_ROW).replace(b"QASR", b"Q\xe9SR"))
    assert [(p.source, p.code) for p in problems] == [(Source.ERP, ProblemCode.ENCODING)]


def test_F02_R1_missing_column_one_problem_per_name() -> None:
    header = ERP_HEADER.replace(",lot_no", "").replace(",uom", "")
    data = "\r\n".join([header, "GRN-4401,2026-09-01,S-101,Q,C,2000,a,b", ""]).encode()
    problems = check_erp(data)
    assert [p.code for p in problems] == [ProblemCode.MISSING_COLUMN] * 2
    assert [p.location for p in problems] == [
        ProblemLocation(column="lot_no"),
        ProblemLocation(column="uom"),
    ]
    assert "lot_no" in problems[0].message


def test_F02_R1_no_data_rows() -> None:
    problems = check_erp(_erp())
    assert [p.code for p in problems] == [ProblemCode.NO_DATA_ROWS]


def test_F02_R1_empty_file_reports_columns_and_no_rows() -> None:
    codes = [p.code for p in check_erp(b"")]
    assert codes == [ProblemCode.MISSING_COLUMN] * 10 + [ProblemCode.NO_DATA_ROWS]


def test_F02_R1_bad_qty_with_line() -> None:
    bad = GOOD_ROW.replace(",2000,", ",abc,")
    problems = check_erp(_erp(GOOD_ROW, GOOD_ROW, GOOD_ROW, GOOD_ROW, bad))
    assert [p.code for p in problems] == [ProblemCode.BAD_NUMBER]
    assert problems[0].location == ProblemLocation(line=5, column="qty")
    assert problems[0].message == 'ERP line 5: qty "abc" is not a number'


@pytest.mark.parametrize("qty", ["", "NaN", "Infinity", "1,000"])
def test_F02_R1_non_finite_or_empty_qty_is_bad_number(qty: str) -> None:
    row = GOOD_ROW.replace(",2000,", f',"{qty}",')
    assert [p.code for p in check_erp(_erp(row))] == [ProblemCode.BAD_NUMBER]


@pytest.mark.parametrize("value", ["01/09/2026", "2026-9-1", "20260901", "2026-02-30", ""])
def test_F02_R1_bad_iso_date(value: str) -> None:
    problems = check_erp(_erp(GOOD_ROW, GOOD_ROW.replace("2026-09-01,S", f"{value},S")))
    assert [p.code for p in problems] == [ProblemCode.BAD_DATE]
    assert problems[0].location == ProblemLocation(line=2, column="grn_date")


def test_F02_R1_every_bad_row_reported_in_line_order() -> None:
    bad_qty = GOOD_ROW.replace(",2000,", ",x,")
    bad_both = bad_qty.replace("2026-09-01,S", "9/1/2026,S")
    problems = check_erp(_erp(bad_qty, GOOD_ROW, bad_both))
    assert [(p.code, p.location and p.location.line) for p in problems] == [
        (ProblemCode.BAD_NUMBER, 1),
        (ProblemCode.BAD_NUMBER, 3),
        (ProblemCode.BAD_DATE, 3),
    ]


# --- R2 sheet ---------------------------------------------------------------------------


def test_F02_R2_sheet_name_for_month() -> None:
    assert sheet_name("2026-09") == "Sep-2026"
    assert sheet_name("2027-01") == "Jan-2027"


def test_F02_R2_pack_sheet_passes(sheet_bytes: bytes) -> None:
    assert check_sheet(PACK_MONTH, sheet_bytes) == []


def test_F02_R2_renamed_worksheet_lists_found_names() -> None:
    problems = check_sheet(PACK_MONTH, _xlsx(title="Aug-2026"))
    assert [p.code for p in problems] == [ProblemCode.MISSING_WORKSHEET]
    assert problems[0].source is Source.SHEET
    assert problems[0].message == "Store sheet: no worksheet named Sep-2026 (found: Aug-2026)"
    assert problems[0].location == ProblemLocation(worksheet="Sep-2026")


def test_F02_R2_missing_header_one_problem_per_name() -> None:
    headers = [h for h in SHEET_HEADERS if h not in ("Lot", "UOM")]
    problems = check_sheet(PACK_MONTH, _xlsx(headers=headers))
    assert [p.code for p in problems] == [ProblemCode.MISSING_HEADER] * 2
    assert [p.location for p in problems] == [
        ProblemLocation(row=1, column="Lot", worksheet="Sep-2026"),
        ProblemLocation(row=1, column="UOM", worksheet="Sep-2026"),
    ]


def test_F02_R2_no_data_rows() -> None:
    problems = check_sheet(PACK_MONTH, _xlsx(rows=0))
    assert [p.code for p in problems] == [ProblemCode.NO_DATA_ROWS]


def test_F02_R2_text_date_not_a_problem() -> None:
    assert check_sheet(PACK_MONTH, _xlsx(rows=3)) == []


def test_F02_R2_not_an_xlsx_is_reported() -> None:
    problems = check_sheet(PACK_MONTH, b"not a workbook")
    assert [(p.source, p.code) for p in problems] == [(Source.SHEET, ProblemCode.ENCODING)]


# --- R3 register ------------------------------------------------------------------------


def test_F02_R3_pack_register_passes(register: list[RegisterFile]) -> None:
    assert check_register(register) == []


@pytest.mark.parametrize("image", [_png(800, 800), _jpeg(1200, 900), _jpeg(800, 1000, sof=0xC2)])
def test_F02_R3_png_and_jpeg_headers_read(image: bytes) -> None:
    assert check_register([RegisterFile(1, "p1", image)]) == []


@pytest.mark.parametrize("image", [_png(799, 1200), _jpeg(1200, 640)])
def test_F02_R3_image_too_small(image: bytes) -> None:
    problems = check_register([RegisterFile(2, "p2.jpg", image)])
    assert [(p.source, p.code) for p in problems] == [
        (Source.REGISTER, ProblemCode.IMAGE_TOO_SMALL)
    ]
    assert problems[0].location == ProblemLocation(page_no=2)


@pytest.mark.parametrize(
    "image",
    [
        b"",
        b"GIF89a" + b"\x00" * 40,
        _png(900, 900)[:20],
        b"\xff\xd8\xff\xe0\x00\x10JFIF",
        _png(0, 900),
    ],
)
def test_F02_R3_image_unreadable(image: bytes) -> None:
    problems = check_register([RegisterFile(1, "p1.jpg", image)])
    assert [p.code for p in problems] == [ProblemCode.IMAGE_UNREADABLE]
    assert problems[0].location == ProblemLocation(page_no=1)


def test_F02_R3_too_many_pages() -> None:
    files = [RegisterFile(p, f"p{p}.png", _png(900, 900)) for p in range(1, 12)]
    assert [p.code for p in check_register(files)] == [ProblemCode.TOO_MANY_PAGES]
    assert check_register(files[:10]) == []


# --- R4 all problems, source order ------------------------------------------------------


def test_F02_R4_pack_passes(
    erp_bytes: bytes, sheet_bytes: bytes, register: list[RegisterFile]
) -> None:
    assert validate_files(PACK_MONTH, erp_bytes, sheet_bytes, register) == []


def test_F02_R4_two_broken_files_both_reported_in_source_order(sheet_bytes: bytes) -> None:
    header = ERP_HEADER.replace(",lot_no", "")
    erp = "\r\n".join([header, "GRN-4401,2026-09-01,S-101,Q,C,2000,PCS,a,b", ""]).encode()
    register = [RegisterFile(1, "p1.png", _png(100, 100)), RegisterFile(2, "p2.png", b"junk")]
    problems = validate_files(PACK_MONTH, erp, _xlsx(title="Aug-2026"), register)
    assert [(p.source, p.code) for p in problems] == [
        (Source.REGISTER, ProblemCode.IMAGE_TOO_SMALL),
        (Source.REGISTER, ProblemCode.IMAGE_UNREADABLE),
        (Source.ERP, ProblemCode.MISSING_COLUMN),
        (Source.SHEET, ProblemCode.MISSING_WORKSHEET),
    ]
