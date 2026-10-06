"""F-06 acceptance criteria (blueprint Section 18; spec US3). Pack processed once per module."""

import copy
from pathlib import Path

import pytest

from app.domain.enums import FlagType, ReviewKind
from app.pipeline.controls import ControlsResult, run_controls
from app.pipeline.engine import EngineInputs
from app.pipeline.reconcile import ReconcileResult, reconcile
from tests.support.normalised import PACK_RUN_ID, normalise_pack
from tests.support.pack import build_engine_inputs


@pytest.fixture(scope="module")
def inputs(pack_dir: Path, ground_truth_dir: Path) -> EngineInputs:
    return build_engine_inputs(pack_dir, ground_truth_dir, PACK_RUN_ID)


@pytest.fixture(scope="module")
def reconciled(inputs: EngineInputs) -> ReconcileResult:
    return reconcile(PACK_RUN_ID, normalise_pack(inputs).rows)


@pytest.fixture(scope="module")
def controls(inputs: EngineInputs, reconciled: ReconcileResult) -> ControlsResult:
    return run_controls(PACK_RUN_ID, inputs.month, reconciled.rows)


def _rows_with(result: ControlsResult, flag: FlagType) -> list[str | None]:
    return [f.source_row_id for f in result.flags if f.flag is flag]


def test_F06_AC1_gap_4429(controls: ControlsResult) -> None:
    gaps = [f for f in controls.flags if f.flag is FlagType.SEQUENCE_GAP]
    assert [(g.grn_no, g.source_row_id, g.detail) for g in gaps] == [
        (4429, None, {"low": "4429", "high": "4429"})
    ]
    items = [i for i in controls.review_items if i.item_key.startswith("FLAG:SEQUENCE_GAP:")]
    assert [(i.kind, i.item_key, i.grn_no) for i in items] == [
        (ReviewKind.FLAG_ACK, "FLAG:SEQUENCE_GAP:4429", 4429)
    ]


def test_F06_AC2_backdated_unusual_user(controls: ControlsResult) -> None:
    assert _rows_with(controls, FlagType.BACKDATED_ENTRY) == ["ERP-0028"]
    assert _rows_with(controls, FlagType.UNUSUAL_USER) == ["ERP-0028"]
    (backdated,) = [f for f in controls.flags if f.flag is FlagType.BACKDATED_ENTRY]
    assert backdated.grn_no == 4430
    assert backdated.detail["grn_date"] == "2026-09-29"
    assert backdated.detail["entered_at"] == "2026-10-02 18:47"
    assert backdated.detail["days_after"] == "3"
    (unusual,) = [f for f in controls.flags if f.flag is FlagType.UNUSUAL_USER]
    assert unusual.detail == {"entered_by": "admin2", "usual": "store.clerk"}
    keys = [i.item_key for i in controls.review_items if i.kind is ReviewKind.FLAG_ACK]
    assert "FLAG:BACKDATED_ENTRY:ERP-0028" in keys


def test_F06_AC3_sunday_dates(controls: ControlsResult) -> None:
    assert _rows_with(controls, FlagType.SUNDAY_DATE) == ["ERP-0015", "XLS-r31"]


def test_F06_AC4_out_of_order(controls: ControlsResult) -> None:
    assert _rows_with(controls, FlagType.OUT_OF_ORDER) == ["XLS-r31"]


def test_F06_AC5_flags_do_not_change_outcomes(
    inputs: EngineInputs, reconciled: ReconcileResult, controls: ControlsResult
) -> None:
    before = copy.deepcopy(reconciled)  # a dataclass: deepcopy, not model_copy
    rows_before = [r.model_dump() for r in before.rows]
    groups_before = [g.model_dump() for g in before.groups]

    run_controls(PACK_RUN_ID, inputs.month, before.rows)

    assert [r.model_dump() for r in before.rows] == rows_before
    assert [g.model_dump() for g in before.groups] == groups_before
    assert [r.model_dump() for r in reconciled.rows] == rows_before
    assert [g.model_dump() for g in reconciled.groups] == groups_before
    assert controls.flags  # flags were raised, yet nothing above moved
