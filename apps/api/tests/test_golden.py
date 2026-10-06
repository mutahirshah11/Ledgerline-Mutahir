"""Golden expectations GE-01 to GE-12 and the Section 8 counts on the full engine (blueprint
Section 8, Section 28; spec US5, FR-036). Every assertion reads what `reconcile_run` stored in a
MemoryRepo, with the ground truth as the register reads. The reconciled runs are shared per module
because these tests only read them."""

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.clock import KARACHI
from app.core.memory_repo import MemoryRepo
from app.domain.enums import (
    CellStatus,
    ComparedField,
    FlagType,
    NormCode,
    Outcome,
    ProofStatus,
    RegisterField,
    RunStatus,
    Source,
)
from app.domain.models import Group
from app.pipeline.engine import EngineInputs, PipelineResult, reconcile_run
from tests.factories import make_run
from tests.support.normalised import PACK_RUN_ID
from tests.support.pack import build_engine_inputs, resolved_cell

NOW = datetime(2026, 10, 15, 8, 30, tzinfo=KARACHI)
REG, ERP, SHEET = Source.REGISTER, Source.ERP, Source.SHEET


@dataclass(frozen=True)
class Reconciled:
    repo: MemoryRepo
    inputs: EngineInputs
    result: PipelineResult

    def group(self, grn: int) -> Group:
        return self.repo.get_group(PACK_RUN_ID, f"GRN:{grn}")

    def flagged_rows(self, flag: FlagType) -> list[str | None]:
        return [f.source_row_id for f in self.repo.list_flags(PACK_RUN_ID) if f.flag is flag]


def _extracted_repo() -> MemoryRepo:
    repo = MemoryRepo()
    run = repo.create_run(make_run(id=PACK_RUN_ID, scenario_now=NOW, created_at=NOW))
    for to in (RunStatus.UPLOADED, RunStatus.VALIDATED, RunStatus.EXTRACTING, RunStatus.EXTRACTED):
        run = repo.transition_run(run.id, to, expected={run.status})
    return repo


@pytest.fixture(scope="module")
def before(pack_dir: Path, ground_truth_dir: Path) -> Reconciled:
    """The pack reconciled once, no review decisions."""
    repo = _extracted_repo()
    inputs = build_engine_inputs(pack_dir, ground_truth_dir, PACK_RUN_ID)
    return Reconciled(repo, inputs, reconcile_run(repo, inputs, now=lambda: NOW))


@pytest.fixture(scope="module")
def after(pack_dir: Path, ground_truth_dir: Path) -> Reconciled:
    """The pack reconciled, then re-reconciled with 4419 → 66 and 4428 → 250 RESOLVED."""
    repo = _extracted_repo()
    first = build_engine_inputs(pack_dir, ground_truth_dir, PACK_RUN_ID)
    reconcile_run(repo, first, now=lambda: NOW)
    decisions = [
        resolved_cell(PACK_RUN_ID, "REG-p2-r05", RegisterField.QTY, "66"),
        resolved_cell(PACK_RUN_ID, "REG-p2-r14", RegisterField.QTY, "250"),
    ]
    inputs = build_engine_inputs(pack_dir, ground_truth_dir, PACK_RUN_ID, decisions)
    return Reconciled(repo, inputs, reconcile_run(repo, inputs, now=lambda: NOW))


def _diff_fields(group: Group) -> list[tuple[ComparedField, Source | None]]:
    return [(d.field, d.outlier) for d in group.diffs]


def test_GE01_duplicate_4409(before: Reconciled) -> None:
    dup = before.repo.get_source_row(PACK_RUN_ID, "XLS-r11")
    assert dup.outcome is Outcome.DUPLICATE
    assert dup.duplicate_of == "XLS-r10"
    assert dup.group_id is None
    group = before.group(4409)
    assert group.outcome is Outcome.MATCHED
    assert group.row_ids[SHEET] == ["XLS-r10"]


def test_GE02_lot_4412(before: Reconciled) -> None:
    group = before.group(4412)
    assert group.outcome is Outcome.MISMATCHED
    assert _diff_fields(group) == [(ComparedField.LOT_NO, ERP)]
    assert group.diffs[0].values == {REG: "L2609-012", ERP: "L2609-021", SHEET: "L2609-012"}


def test_GE03_date_4415(before: Reconciled) -> None:
    group = before.group(4415)
    assert group.outcome is Outcome.MISMATCHED
    assert _diff_fields(group) == [(ComparedField.DATE, ERP)]
    assert group.diffs[0].values == {REG: "2026-09-12", ERP: "2026-09-13", SHEET: "2026-09-12"}
    assert "ERP-0015" in before.flagged_rows(FlagType.SUNDAY_DATE)


def test_GE04_text_date_4416(before: Reconciled) -> None:
    row = before.repo.get_source_row(PACK_RUN_ID, "XLS-r18")
    assert row.date == date(2026, 9, 14)
    log = before.repo.list_norm_log(PACK_RUN_ID, "XLS-r18")
    assert [(n.field, n.code, n.before, n.after) for n in log if n.field == "date"] == [
        ("date", NormCode.DATE_TEXT_PARSED, "9/14/2026", "2026-09-14")
    ]
    assert before.group(4416).outcome is Outcome.MATCHED


def test_GE05_qty_4417(before: Reconciled) -> None:
    group = before.group(4417)
    assert group.outcome is Outcome.MISMATCHED
    assert _diff_fields(group) == [(ComparedField.QTY, ERP)]
    assert group.diffs[0].values == {REG: "48 CTN", ERP: "46 CTN", SHEET: "48 CTN"}


def test_GE06_correction_4419(before: Reconciled, after: Reconciled) -> None:
    (page,) = [p for p in before.inputs.register_pages if p.page_no == 2]
    (merged,) = [r for r in page.rows if r.row_index == 5]
    cell = merged.cells[RegisterField.QTY].merged
    assert (cell.status, cell.struck, cell.value) == (CellStatus.CORRECTED, "60", "66")

    row = before.repo.get_source_row(PACK_RUN_ID, "REG-p2-r05")
    assert row.cell_status["qty"] is CellStatus.CORRECTED
    assert before.group(4419).outcome is Outcome.NEEDS_REVIEW

    resolved = after.repo.get_source_row(PACK_RUN_ID, "REG-p2-r05")
    assert resolved.qty == Decimal("66")
    group = after.group(4419)
    assert group.outcome is Outcome.MISMATCHED
    assert _diff_fields(group) == [(ComparedField.QTY, SHEET)]
    assert group.diffs[0].values == {REG: "66 DRUM", ERP: "66 DRUM", SHEET: "60 DRUM"}
    # Blueprint GE-06 / T069 do not state the cell status after RESOLVED review (only that it
    # starts CORRECTED); not asserted here.


def test_GE07_missing_from_erp_4421(before: Reconciled) -> None:
    group = before.group(4421)
    assert group.outcome is Outcome.MISSING
    assert group.missing_from == [ERP]
    assert group.row_ids == {REG: ["REG-p2-r07"], SHEET: ["XLS-r23"]}


def test_GE08_units_4425(before: Reconciled) -> None:
    reg = before.repo.get_source_row(PACK_RUN_ID, "REG-p2-r11")
    erp = before.repo.get_source_row(PACK_RUN_ID, "ERP-0024")
    sheet = before.repo.get_source_row(PACK_RUN_ID, "XLS-r27")
    assert (reg.qty_base, reg.base_uom) == (Decimal("1097.28"), "M")
    assert (erp.qty_base, erp.base_uom) == (Decimal("1097.28"), "M")
    assert (sheet.qty_base, sheet.base_uom) == (Decimal("1200.00"), "M")
    group = before.group(4425)
    assert group.outcome is Outcome.MISMATCHED
    assert _diff_fields(group) == [(ComparedField.QTY, SHEET)]
    assert group.diffs[0].compared == {REG: "1097.28 M", ERP: "1097.28 M", SHEET: "1200.00 M"}


def test_GE09_ink_blot_4428(before: Reconciled) -> None:
    row = before.repo.get_source_row(PACK_RUN_ID, "REG-p2-r14")
    assert row.cell_status["qty"] is CellStatus.ILLEGIBLE
    assert row.qty is None
    assert row.qty_base is None
    assert "250" not in row.raw.values()
    assert before.group(4428).outcome is Outcome.NEEDS_REVIEW


def test_GE10_gap_4429(before: Reconciled) -> None:
    gaps = [f for f in before.repo.list_flags(PACK_RUN_ID) if f.flag is FlagType.SEQUENCE_GAP]
    assert [g.grn_no for g in gaps] == [4429]
    assert all(g.grn_no != 4429 for g in before.repo.list_groups(PACK_RUN_ID))


def test_GE11_backdated_4430(before: Reconciled) -> None:
    group = before.group(4430)
    assert group.outcome is Outcome.MISSING
    assert group.missing_from == [REG, SHEET]
    assert group.row_ids == {ERP: ["ERP-0028"]}
    assert before.flagged_rows(FlagType.BACKDATED_ENTRY) == ["ERP-0028"]
    assert before.flagged_rows(FlagType.UNUSUAL_USER) == ["ERP-0028"]


def test_GE12_return_row(before: Reconciled) -> None:
    row = before.repo.get_source_row(PACK_RUN_ID, "XLS-r31")
    assert row.outcome is Outcome.NON_RECEIPT
    assert row.group_id is None
    assert all(
        "XLS-r31" not in ids
        for g in before.repo.list_groups(PACK_RUN_ID)
        for ids in g.row_ids.values()
    )
    # F-06.AC3: SUNDAY_DATE on ERP-0015 and XLS-r31 only (asserted exactly here for both GE-03
    # and GE-12).
    assert before.flagged_rows(FlagType.SUNDAY_DATE) == ["ERP-0015", "XLS-r31"]
    assert before.flagged_rows(FlagType.OUT_OF_ORDER) == ["XLS-r31"]


def _stored_group_counts(r: Reconciled) -> Counter[Outcome]:
    return Counter(g.outcome for g in r.repo.list_groups(PACK_RUN_ID))


def _stored_row_counts(r: Reconciled) -> dict[Outcome, dict[Source, int]]:
    counts: dict[Outcome, dict[Source, int]] = {o: dict.fromkeys(Source, 0) for o in Outcome}
    for row in r.repo.list_source_rows(PACK_RUN_ID):
        assert row.outcome is not None
        counts[row.outcome][row.source] += 1
    return counts


def _zero_filled(**by_name: int) -> dict[Outcome, int]:
    return {o: by_name.get(o.name, 0) for o in Outcome}


def _rows(**by_name: tuple[int, int, int]) -> dict[Outcome, dict[Source, int]]:
    return {o: dict(zip(Source, by_name.get(o.name, (0, 0, 0)), strict=True)) for o in Outcome}


def test_counts_before_and_after_review(before: Reconciled, after: Reconciled) -> None:
    groups_before = _zero_filled(MATCHED=21, MISMATCHED=4, NEEDS_REVIEW=2, MISSING=2)
    rows_before = _rows(
        MATCHED=(21, 21, 21),
        MISMATCHED=(4, 4, 4),
        NEEDS_REVIEW=(2, 2, 2),
        MISSING=(1, 1, 1),
        DUPLICATE=(0, 0, 1),
        NON_RECEIPT=(0, 0, 1),
    )
    assert before.result.counts.groups_by_outcome == groups_before
    assert before.result.counts.rows_by_outcome == rows_before
    assert _stored_group_counts(before) == +Counter(groups_before)
    assert _stored_row_counts(before) == rows_before
    assert sum(groups_before.values()) == 29
    assert [sum(n[s] for n in rows_before.values()) for s in Source] == [28, 28, 30]
    before_proof = before.repo.get_proof(PACK_RUN_ID)
    assert before_proof is not None
    assert before_proof.status is ProofStatus.PASS

    groups_after = _zero_filled(MATCHED=22, MISMATCHED=5, MISSING=2)
    assert after.result.counts.groups_by_outcome == groups_after
    assert _stored_group_counts(after) == +Counter(groups_after)
    after_proof = after.repo.get_proof(PACK_RUN_ID)
    assert after_proof is not None
    assert after_proof.status is ProofStatus.PASS


def _no_flags(r: Reconciled, *row_ids: str) -> bool:
    return all(f.source_row_id not in row_ids for f in r.repo.list_flags(PACK_RUN_ID))


def test_not_a_problem_items_unflagged(before: Reconciled) -> None:
    """SC-006 / blueprint Section 8 "not a problem" list: "H202" (GRN 4408, OCR for H2O2), thread
    cones 40/2 received in CONE (GRN 4404) and in CTN (GRN 4417, each unit consistent across its
    own sources), and item text differences (e.g. "POLYBAGS 12X16 (BUNDLES)") are never flagged
    and never produce a unit/qty/item diff beyond the planted GE-05 qty mismatch on 4417."""
    # "H202" (GRN 4408): register "H202 50%" vs ERP/sheet "H2O2 50%" -- OCR text only, MATCHED.
    h202 = before.group(4408)
    assert h202.outcome is Outcome.MATCHED
    assert h202.diffs == []
    assert _no_flags(before, "REG-p1-r08", "ERP-0008", "XLS-r9")
    reg_h202 = before.repo.get_source_row(PACK_RUN_ID, "REG-p1-r08")
    erp_h202 = before.repo.get_source_row(PACK_RUN_ID, "ERP-0008")
    assert reg_h202.item == "H202 50%"
    assert erp_h202.item is not None and "H2O2" in erp_h202.item

    # Thread cones 40/2 in CONE (GRN 4404): all three sources agree on unit CONE, MATCHED.
    cone = before.group(4404)
    assert cone.outcome is Outcome.MATCHED
    assert cone.diffs == []
    assert _no_flags(before, "REG-p1-r04", "ERP-0004", "XLS-r5")
    assert before.repo.get_source_row(PACK_RUN_ID, "ERP-0004").uom == "CONE"

    # Thread cones 40/2 in CTN (GRN 4417, GE-05): ERP qty differs (46 vs 48), already flagged
    # as a qty mismatch, but the CTN unit itself is consistent across sources -- no uom diff.
    ctn = before.group(4417)
    assert ctn.outcome is Outcome.MISMATCHED
    assert _diff_fields(ctn) == [(ComparedField.QTY, ERP)]
    assert ComparedField.UOM not in {d.field for d in ctn.diffs}
    assert _no_flags(before, "REG-p2-r03", "ERP-0017", "XLS-r19")
    assert {
        before.repo.get_source_row(PACK_RUN_ID, rid).uom
        for rid in ("REG-p2-r03", "ERP-0017", "XLS-r19")
    } == {"CTN"}

    # Item text differences (e.g. ERP "POLYBAGS 12X16 (BUNDLES)" vs sheet casing) are never a
    # diff: ComparedField has no item/description member, so no group's diffs can carry one.
    assert all(d.field is not None for g in before.repo.list_groups(PACK_RUN_ID) for d in g.diffs)
    bundles_erp = before.repo.get_source_row(PACK_RUN_ID, "ERP-0027")
    assert bundles_erp.item == "POLYBAGS 12X16 (BUNDLES)"
