"""Phase 4 read endpoints against specs/005-review-delivery/contracts/openapi.yaml (005 T035,
T064): EP-11 groups, EP-12 flags, EP-13 proof and the EP-05 `RunView` (FR-020, FR-021, C25).

`test_api_phase4_contract_paths` and `test_phase4_endpoints_401_without_token` check the app's
real routes (EP-11 to EP-18) against the committed copy of the Phase 4 contract at
`tests/contracts/openapi_phase4.yaml` (specs/ is git-ignored, same convention as
`tests/contracts/openapi_phase3.yaml`)."""

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.domain.enums import FlagType, Source
from app.domain.models import ControlFlag, GroupView, Proof, ReportLinks, ReviewItem, RunView
from tests.support.pack_run import PackRun
from tests.support.routes import api_routes

API = "/api/v1"
CONTRACT_PATH = Path(__file__).parent / "contracts" / "openapi_phase4.yaml"


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


# --- T064: contract paths/status codes, 200-body parsing, 401 without a token -------------


def _normalise(path: str) -> str:
    """Path params by position: `{id}` vs `{item_id}` compare equal, names don't matter."""
    segs = ("{}" if seg.startswith("{") and seg.endswith("}") else seg for seg in path.split("/"))
    return "/".join(segs)


def _load_phase4_spec() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8")))


@pytest.fixture
def app() -> FastAPI:
    from app.main import create_app

    return create_app()


def test_api_phase4_contract_paths(app: FastAPI) -> None:
    """EP-11 to EP-18: the app's real routes match the committed Phase 4 contract copy
    (method, path), and every documented status code of each operation appears in the app's
    `openapi()` responses for that operation."""
    spec = _load_phase4_spec()
    expected = {
        (method.upper(), _normalise(API + path))
        for path, ops in spec["paths"].items()
        for method in ops
        if method in {"get", "post", "put", "patch", "delete"}
    }
    actual = {
        (method, _normalise(route.path)) for route in api_routes(app) for method in route.methods
    }
    missing_in_app = expected - actual
    assert not missing_in_app, f"declared in openapi.yaml but not routed: {missing_in_app}"

    schema = app.openapi()
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            real_path = next(p for p in schema["paths"] if _normalise(p) == _normalise(API + path))
            real_responses = schema["paths"][real_path][method]["responses"]
            for status in op["responses"]:
                assert status in real_responses, f"{method.upper()} {path} missing {status}"


def test_phase4_200_bodies_parse_into_domain_models(
    pack_run: PackRun, api_client_as: Callable[[str], TestClient]
) -> None:
    """A light check (T064): the EP-05, EP-11, EP-12, EP-13, EP-14 and EP-18 200 bodies of
    `pack_run` parse into their `domain/models.py` shapes."""
    api = api_client_as("controller")

    run_resp = api.get(f"{API}/runs/{pack_run.run_id}")
    assert run_resp.status_code == 200
    RunView.model_validate(run_resp.json())

    groups_resp = api.get(f"{API}/runs/{pack_run.run_id}/groups")
    assert groups_resp.status_code == 200
    for g in groups_resp.json():
        GroupView.model_validate(g)

    flags_resp = api.get(f"{API}/runs/{pack_run.run_id}/flags")
    assert flags_resp.status_code == 200
    for f in flags_resp.json():
        ControlFlag.model_validate(f)

    proof_resp = api.get(f"{API}/runs/{pack_run.run_id}/proof")
    assert proof_resp.status_code == 200
    Proof.model_validate(proof_resp.json())

    items_resp = api.get(f"{API}/runs/{pack_run.run_id}/review-items")
    assert items_resp.status_code == 200
    item_fields = set(ReviewItem.model_fields) - {"context"}  # EP-14's context is view-only
    for item in items_resp.json():
        assert item_fields <= set(item)

    finalise_resp = api.post(f"{API}/runs/{pack_run.run_id}/finalise", json={"confirm": True})
    assert finalise_resp.status_code == 200

    report_resp = api.get(f"{API}/runs/{pack_run.run_id}/report")
    assert report_resp.status_code == 200
    ReportLinks.model_validate(report_resp.json())


def test_phase4_endpoints_401_without_token(client: TestClient, pack_run: PackRun) -> None:
    """EP-11 to EP-18 without a bearer token are 401 `UNAUTHENTICATED` (contracts/auth.md)."""
    run_id = pack_run.run_id
    item_id = pack_run.repo.list_review_items(run_id)[0].id
    calls: list[tuple[str, str, dict[str, Any] | None]] = [
        ("GET", f"{API}/runs/{run_id}/groups", None),
        ("GET", f"{API}/runs/{run_id}/flags", None),
        ("GET", f"{API}/runs/{run_id}/proof", None),
        ("GET", f"{API}/runs/{run_id}/review-items", None),
        ("POST", f"{API}/review-items/{item_id}/resolve", {"action": "ACCEPT_READ"}),
        ("GET", f"{API}/runs/{run_id}/pages/1/image", None),
        ("POST", f"{API}/runs/{run_id}/finalise", {"confirm": True}),
        ("GET", f"{API}/runs/{run_id}/report", None),
    ]
    for method, path, body in calls:
        resp = client.request(method, path, json=body)
        assert resp.status_code == 401, f"{method} {path} -> {resp.status_code}"
        assert resp.json()["error"]["code"] == "UNAUTHENTICATED"
