"""Build the replace_reconciliation payloads used by supabase/tests/smoke.sql.

The payloads are real `ReconciliationPayload` models dumped with `model_dump(mode="json")`, so
the database is tested against exactly the JSON shape the API sends. Ids are deterministic
(uuid5), the way the Phase 1 engine's `stable_id` makes them, so a re-reconcile re-uses ids.

    uv run --project apps/api python supabase/tests/make_payload.py          # write fixtures
    uv run --project apps/api python supabase/tests/make_payload.py --check  # fail on drift

`apps/api/tests/test_migration_smoke_fixtures.py` runs the --check comparison in CI.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
API_ROOT = HERE.parents[1] / "apps" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from app.domain.enums import (  # noqa: E402
    CellStatus,
    ComparedField,
    FlagType,
    NormCode,
    Outcome,
    RowKind,
    Source,
)
from app.domain.models import (  # noqa: E402
    CanonicalRow,
    ControlFlag,
    Diff,
    Group,
    NormLogEntry,
    ReconciliationPayload,
    RowMeta,
)

FIXTURES = HERE / "fixtures"
KARACHI = ZoneInfo("Asia/Karachi")
RUN_ID = uuid5(NAMESPACE_URL, "https://ledgerline.example/smoke/run")


def sid(*parts: str) -> UUID:
    """Deterministic id within the smoke run (like the engine's stable_id)."""
    return uuid5(RUN_ID, ":".join(parts))


def gid(grn: int) -> UUID:
    return sid("group", f"GRN:{grn}")


def row(
    source_row_id: str,
    *,
    grn: int | None,
    qty: str | None,
    uom: str | None,
    qty_base: str | None = None,
    base_uom: str | None = None,
    group: int | None = None,
    outcome: Outcome | None = Outcome.MATCHED,
    row_kind: RowKind = RowKind.RECEIPT,
    duplicate_of: str | None = None,
    lot_no: str | None = "L2609-002",
    supplier: str | None = "S-102",
    day: int = 12,
) -> CanonicalRow:
    source = {"REG": Source.REGISTER, "ERP": Source.ERP, "XLS": Source.SHEET}[
        source_row_id.split("-")[0]
    ]
    if source is Source.REGISTER:
        page, slot = source_row_id.split("-")[1:]
        meta = RowMeta(page_no=int(page[1:]), row_index=int(slot[1:]), received_by="store.clerk")
        cell_status = {
            "date": CellStatus.CLEAR,
            "grn_no": CellStatus.CLEAR,
            "qty": CellStatus.CORRECTED if source_row_id == "REG-p2-r14" else CellStatus.CLEAR,
        }
    elif source is Source.ERP:
        meta = RowMeta(
            entered_by="store.clerk",
            entered_at=datetime(2026, 9, day, 17, 5, tzinfo=KARACHI),
        )
        cell_status = {"date": CellStatus.CLEAR, "qty": CellStatus.CLEAR}
    else:
        meta = RowMeta(remarks="checked")
        cell_status = {"date": CellStatus.CLEAR, "qty": CellStatus.CLEAR}
    return CanonicalRow(
        id=sid("row", source_row_id),
        run_id=RUN_ID,
        source=source,
        source_row_id=source_row_id,
        row_kind=row_kind,
        grn_no=grn,
        date=date(2026, 9, day),
        supplier_code=supplier,
        item="Cotton twill 60in",
        lot_no=lot_no,
        qty=None if qty is None else Decimal(qty),
        uom=uom,
        qty_base=Decimal(qty_base if qty_base is not None else qty) if qty is not None else None,
        base_uom=base_uom or uom,
        cell_status=cell_status,
        raw={"qty": None if qty is None else f"{qty} {uom}", "grn_no": f"GRN-{grn}"},
        meta=meta,
        duplicate_of=duplicate_of,
        group_id=None if group is None else gid(group),
        outcome=outcome,
    )


def group(
    grn: int,
    outcome: Outcome,
    row_ids: dict[Source, list[str]],
    *,
    missing_from: list[Source] | None = None,
    diffs: list[Diff] | None = None,
) -> Group:
    return Group(
        id=gid(grn),
        run_id=RUN_ID,
        group_key=f"GRN:{grn}",
        grn_no=grn,
        row_ids={k: sorted(v) for k, v in row_ids.items()},
        outcome=outcome,
        missing_from=missing_from or [],
        diffs=diffs or [],
    )


def flag(
    name: str, kind: FlagType, *, grn: int | None, row_id: str | None, **detail: str
) -> ControlFlag:
    return ControlFlag(
        id=sid("flag", name),
        run_id=RUN_ID,
        flag=kind,
        grn_no=grn,
        source_row_id=row_id,
        detail=detail,
    )


def norm(row_id: str, field: str, code: NormCode, before: str, after: str) -> NormLogEntry:
    return NormLogEntry(
        id=sid("norm", row_id, field, code.value),
        run_id=RUN_ID,
        source_row_id=row_id,
        field=field,
        code=code,
        before=before,
        after=after,
    )


def payload_a() -> ReconciliationPayload:
    """First reconcile: a qty mismatch, a unit conversion, a missing sheet row, a duplicate."""
    mismatch = Diff(
        field=ComparedField.QTY,
        values={Source.REGISTER: "40 KG", Source.ERP: "40 KG", Source.SHEET: "45 KG"},
        compared={Source.REGISTER: "40 KG", Source.ERP: "40 KG", Source.SHEET: "45 KG"},
        outlier=Source.SHEET,
    )
    rows = [
        row("REG-p2-r14", grn=4425, qty="40", uom="KG", group=4425, outcome=Outcome.MISMATCHED),
        row("ERP-0027", grn=4425, qty="40.00", uom="KG", group=4425, outcome=Outcome.MISMATCHED),
        row("XLS-r30", grn=4425, qty="45", uom="KG", group=4425, outcome=Outcome.MISMATCHED),
        row(
            "REG-p2-r15",
            grn=4426,
            qty="1200",
            uom="YD",
            qty_base="1097.28",
            base_uom="M",
            group=4426,
            outcome=Outcome.MATCHED_AFTER_CONVERSION,
            day=13,
        ),
        row(
            "ERP-0028",
            grn=4426,
            qty="1097.28",
            uom="M",
            group=4426,
            outcome=Outcome.MATCHED_AFTER_CONVERSION,
            day=13,
        ),
        row(
            "XLS-r31",
            grn=4426,
            qty="1097.28",
            uom="M",
            group=4426,
            outcome=Outcome.MATCHED_AFTER_CONVERSION,
            day=13,
        ),
        row(
            "REG-p2-r16",
            grn=4427,
            qty="12.5",
            uom="KG",
            group=4427,
            outcome=Outcome.MISSING,
            day=14,
        ),
        row(
            "ERP-0029", grn=4427, qty="12.50", uom="KG", group=4427, outcome=Outcome.MISSING, day=14
        ),
        row(
            "ERP-0030",
            grn=4427,
            qty="12.50",
            uom="KG",
            group=None,
            outcome=Outcome.DUPLICATE,
            row_kind=RowKind.DUPLICATE,
            duplicate_of="ERP-0029",
            day=14,
        ),
    ]
    groups = [
        group(
            4425,
            Outcome.MISMATCHED,
            {Source.REGISTER: ["REG-p2-r14"], Source.ERP: ["ERP-0027"], Source.SHEET: ["XLS-r30"]},
            diffs=[mismatch],
        ),
        group(
            4426,
            Outcome.MATCHED_AFTER_CONVERSION,
            {Source.REGISTER: ["REG-p2-r15"], Source.ERP: ["ERP-0028"], Source.SHEET: ["XLS-r31"]},
        ),
        group(
            4427,
            Outcome.MISSING,
            {Source.REGISTER: ["REG-p2-r16"], Source.ERP: ["ERP-0029"]},
            missing_from=[Source.SHEET],
        ),
    ]
    flags = [
        flag("sunday-4427", FlagType.SUNDAY_DATE, grn=4427, row_id="REG-p2-r16", date="2026-09-13"),
        flag(
            "backdated-0029",
            FlagType.BACKDATED_ENTRY,
            grn=4427,
            row_id="ERP-0029",
            entered_at="2026-10-02 18:47",
            usual="store.clerk",
        ),
    ]
    norm_log = [
        norm("ERP-0027", "grn_no", NormCode.GRN_PREFIX_STRIPPED, "GRN-4425", "4425"),
        norm("REG-p2-r15", "qty", NormCode.UNIT_CONVERTED, "1200 YD", "1097.28 M"),
        norm("XLS-r30", "supplier_code", NormCode.SUPPLIER_MAPPED, "Fabrico Ltd", "S-102"),
    ]
    return ReconciliationPayload(rows=rows, groups=groups, flags=flags, norm_log=norm_log)


def payload_b() -> ReconciliationPayload:
    """Re-reconcile after review: 4425 now matches, 4427 and its rows are gone, 4428 is new."""
    a = payload_a()
    keep = {"REG-p2-r14", "ERP-0027", "REG-p2-r15", "ERP-0028", "XLS-r31"}
    rows = [
        r.model_copy(update={"outcome": Outcome.MATCHED}) if r.grn_no == 4425 else r
        for r in a.rows
        if r.source_row_id in keep
    ]
    rows.insert(2, row("XLS-r30", grn=4425, qty="40", uom="KG", group=4425))
    rows += [
        row(
            "ERP-0031",
            grn=4428,
            qty="7",
            uom="KG",
            group=4428,
            outcome=Outcome.MISSING,
            lot_no="L2609-007",
            day=15,
        ),
        row(
            "XLS-r32",
            grn=4428,
            qty="7",
            uom="KG",
            group=4428,
            outcome=Outcome.MISSING,
            lot_no="L2609-007",
            day=15,
        ),
    ]
    groups = [
        group(
            4425,
            Outcome.MATCHED,
            {Source.REGISTER: ["REG-p2-r14"], Source.ERP: ["ERP-0027"], Source.SHEET: ["XLS-r30"]},
        ),
        a.groups[1],
        group(
            4428,
            Outcome.MISSING,
            {Source.ERP: ["ERP-0031"], Source.SHEET: ["XLS-r32"]},
            missing_from=[Source.REGISTER],
        ),
    ]
    flags = [
        flag("sunday-4428", FlagType.SUNDAY_DATE, grn=4428, row_id="ERP-0031", date="2026-09-15"),
    ]
    norm_log = [a.norm_log[1], a.norm_log[0]]  # order differs from A on purpose (seq order)
    return ReconciliationPayload(rows=rows, groups=groups, flags=flags, norm_log=norm_log)


def render(payload: ReconciliationPayload) -> str:
    data: Any = payload.model_dump(mode="json")
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def fixtures() -> dict[str, str]:
    return {"payload_a.json": render(payload_a()), "payload_b.json": render(payload_b())}


def main(argv: list[str]) -> int:
    check = "--check" in argv
    stale = []
    for name, text in fixtures().items():
        path = FIXTURES / name
        if check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                stale.append(name)
        else:
            FIXTURES.mkdir(exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
            print(f"wrote {path}")
    if stale:
        print(f"stale fixtures (re-run make_payload.py): {', '.join(stale)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
