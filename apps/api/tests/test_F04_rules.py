"""F-04 rules R1 to R9 and the normalisation log (contracts/normalise.md N1 to N14)."""

from pathlib import Path

from app.pipeline.normalise import read_erp, read_sheet

PACK_MONTH = "2026-09"


def test_F04_R1_row_ids(pack_dir: Path) -> None:
    erp = read_erp((pack_dir / "t4_erp_grn.csv").read_bytes())
    sheet = read_sheet((pack_dir / "t4_store_sheet.xlsx").read_bytes(), PACK_MONTH)

    assert [r.source_row_id for r in erp] == [f"ERP-{n:04}" for n in range(1, 29)]
    assert [r.line_no for r in erp] == list(range(1, 29))
    assert erp[23].values["qty"] == "1097.28"
    assert erp[23].values["item_desc"] == 'GREY FABRIC 63"'
    assert [r.source_row_id for r in sheet] == [f"XLS-r{n}" for n in range(2, 32)]
    assert sheet[16].values["Date"] == "9/14/2026"
    assert sheet[29].values["Remarks"] == "Returned to supplier (torn)"
