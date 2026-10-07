"""Phase 4 read endpoints against specs/005-review-delivery/contracts/openapi.yaml (005 T035):
EP-11 groups, EP-12 flags, EP-13 proof and the EP-05 `RunView` (FR-020, FR-021, C25)."""

from collections.abc import Callable
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.enums import FlagType, Source
from app.domain.models import ControlFlag, GroupView, Proof, RunView
from tests.support.pack_run import PackRun

API = "/api/v1"


def test_EP11_groups_filter_mismatched_rows_and_diffs(
    pack_run: PackRun, api_client_as: Callable[[str], TestClient]
) -> None:
    api = api_client_as("reviewer")
    resp = api.get(f"{API}/runs/{pack_run.run_id}/groups", params={"outcome": "MISMATCHED"})
    assert resp.status_code == 200
    views = [GroupView.model_validate(v) for v in resp.json()]
    assert [v.group.grn_no for v in views] == [4412, 4415, 4417, 4425]
    for v in views:
        assert list(v.rows) == [Source.REGISTER, Source.ERP, Source.SHEET]
        for source, rows in v.rows.items():
            assert [r.source_row_id for r in rows] == v.group.row_ids[source]
            assert all(r.source is source and r.raw for r in rows)
        assert v.group.diffs and v.group.diffs[0].outlier is not None
    sunday = next(v for v in views if v.group.grn_no == 4415).rows[Source.ERP][0]
    assert sunday.flags == [FlagType.SUNDAY_DATE] and str(sunday.date) == "2026-09-13"
    every = api.get(f"{API}/runs/{pack_run.run_id}/groups").json()
    assert len(every) == 29


def test_EP12_flags_current_reconcile(
    pack_run: PackRun, api_client_as: Callable[[str], TestClient]
) -> None:
    resp = api_client_as("ops").get(f"{API}/runs/{pack_run.run_id}/flags")
    assert resp.status_code == 200
    flags = [ControlFlag.model_validate(f) for f in resp.json()]
    assert flags == pack_run.repo.list_flags(pack_run.run_id)
    assert len(flags) == 6


def test_EP13_proof_section19_shape(
    pack_run: PackRun, api_client_as: Callable[[str], TestClient]
) -> None:
    resp = api_client_as("reviewer").get(f"{API}/runs/{pack_run.run_id}/proof")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"run_id", "status", "checks", "computed_at"}
    assert set(body["checks"]) == {"C1", "C2", "C3", "C4", "C5"}
    assert body["checks"]["C2"]["pass"] is True and body["checks"]["C2"]["rows"] == 86
    assert Proof.model_validate(body) == pack_run.repo.get_proof(pack_run.run_id)


def test_EP05_run_view_fields(
    pack_run: PackRun, api_client_as: Callable[[str], TestClient]
) -> None:
    resp = api_client_as("controller").get(f"{API}/runs/{pack_run.run_id}")
    assert resp.status_code == 200
    view = RunView.model_validate(resp.json())
    assert view.headline == "29 receipts: 21 match, 8 need attention"
    assert len(view.exceptions) == 11 and view.exceptions[0].grn_no == 4421
    assert view.summary is None  # before finalise
    assert view.proof_status is not None and view.open_review_items == 4
    assert [s.stage.value for s in view.stage_progress.stages] == [
        "upload", "validate", "extract", "reconcile", "proof"
    ]  # fmt: skip


@pytest.mark.parametrize("path", ["groups", "flags", "proof"])
def test_EP11_to_EP13_unknown_run_404(
    path: str, api_client_as: Callable[[str], TestClient]
) -> None:
    resp = api_client_as("reviewer").get(f"{API}/runs/{uuid4()}/{path}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
