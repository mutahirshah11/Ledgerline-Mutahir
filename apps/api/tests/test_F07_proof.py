"""F-07 acceptance criteria on the pack (blueprint Section 19; spec US4). AC3 runs through the
engine (`reconcile_run`) and is written with US5."""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

import pytest

import app.pipeline.normalise as normalise
from app.core import clock
from app.domain.enums import ProofStatus, Source
from app.domain.models import CanonicalRow, ControlFlag, Group, Proof, ReviewItem
from app.pipeline.controls import run_controls
from app.pipeline.engine import EngineInputs
from app.pipeline.proof import prove
from app.pipeline.reconcile import reconcile
from tests.support.normalised import PACK_RUN_ID, normalise_pack
from tests.support.pack import build_engine_inputs, resolved_cell


@dataclass(frozen=True)
class _Pipeline:
    rows_in: dict[Source, int]
    rows: list[CanonicalRow]
    groups: list[Group]
    flags: list[ControlFlag]


def _pipeline(inputs: EngineInputs, decisions: Sequence[ReviewItem] = ()) -> _Pipeline:
    """F-04 → F-05 → F-06 on the pack; the parsers are looked up on the module so a test can
    monkeypatch them."""
    normalised = normalise_pack(
        inputs,
        erp=normalise.read_erp(inputs.erp_bytes),
        sheet=normalise.read_sheet(inputs.sheet_bytes, inputs.month),
        decisions=decisions,
    )
    reconciled = reconcile(PACK_RUN_ID, normalised.rows)
    controls = run_controls(PACK_RUN_ID, inputs.month, reconciled.rows)
    counts = Counter(r.source for r in normalised.rows)
    return _Pipeline(
        rows_in={s: counts[s] for s in Source},
        rows=reconciled.rows,
        groups=reconciled.groups,
        flags=[*normalised.flags, *controls.flags],
    )


def _prove(
    inputs: EngineInputs, p: _Pipeline, now: datetime, groups: list[Group] | None = None
) -> Proof:
    return prove(
        PACK_RUN_ID, inputs, p.rows_in, p.rows, p.groups if groups is None else groups, p.flags, now
    )


@pytest.fixture(scope="module")
def inputs(pack_dir: Path, ground_truth_dir: Path) -> EngineInputs:
    return build_engine_inputs(pack_dir, ground_truth_dir, PACK_RUN_ID)


@pytest.fixture(scope="module")
def pack(inputs: EngineInputs) -> _Pipeline:
    return _pipeline(inputs)


def test_F07_AC1_pack_pass(inputs: EngineInputs, pack: _Pipeline, frozen_clock: datetime) -> None:
    result = _prove(inputs, pack, clock.now())

    assert result.status is ProofStatus.PASS
    c1 = result.checks.C1.per_source
    assert {s: (c.in_, c.placed) for s, c in c1.items()} == {
        Source.REGISTER: (28, 28),
        Source.ERP: (28, 28),
        Source.SHEET: (30, 30),
    }
    assert result.checks.C2.rows == 86
    assert result.checks.C3.per_source == {Source.REGISTER: 28, Source.ERP: 28, Source.SHEET: 30}
    assert (result.checks.C5.grns, result.checks.C5.gaps) == (29, [4429])
    assert result.computed_at == frozen_clock


def test_F07_AC2_qty_unknown_until_resolved(
    inputs: EngineInputs, pack: _Pipeline, frozen_clock: datetime
) -> None:
    before = _prove(inputs, pack, frozen_clock).checks.C4
    assert "REG-p2-r14" in before.qty_unknown
    assert before.pass_

    decision = resolved_cell(PACK_RUN_ID, "REG-p2-r14", "qty", "250")
    decided = replace(inputs, decisions=[decision])
    after = _prove(decided, _pipeline(decided, [decision]), frozen_clock).checks.C4

    assert "REG-p2-r14" not in after.qty_unknown
    assert after.pass_


def test_F07_AC4_row_in_two_groups(
    inputs: EngineInputs, pack: _Pipeline, frozen_clock: datetime
) -> None:
    first, second, *rest = pack.groups
    source, ids = next(iter(first.row_ids.items()))
    moved = ids[0]
    twice = second.model_copy(
        update={"row_ids": {**second.row_ids, source: [*second.row_ids.get(source, []), moved]}}
    )

    result = _prove(inputs, pack, frozen_clock, groups=[first, twice, *rest])

    assert not result.checks.C2.pass_
    assert result.checks.C2.placed_twice == [moved]
    assert result.status is ProofStatus.FAIL


def test_F07_AC5_parser_skips_row(
    inputs: EngineInputs, monkeypatch: pytest.MonkeyPatch, frozen_clock: datetime
) -> None:
    real = normalise.read_sheet
    monkeypatch.setattr(normalise, "read_sheet", lambda data, month: real(data, month)[:-1])

    result = _prove(inputs, _pipeline(inputs), frozen_clock)

    assert result.checks.C1.pass_  # the parser and the placement agree with each other ...
    assert not result.checks.C3.pass_  # ... but not with the independent count
    assert result.checks.C3.per_source[Source.SHEET] == 30
    assert result.checks.C1.per_source[Source.SHEET].in_ == 29
    assert result.status is ProofStatus.FAIL
