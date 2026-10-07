"""T067: the app's real routes against the committed OpenAPI contract copy.

`specs/` is git-ignored, so the contract lives at `tests/contracts/openapi_phase3.yaml`, a copy of
`specs/004-api-backbone/contracts/openapi.yaml` (keep them identical; if they drift, prefer fixing
the spec copy only when the local spec file is itself stale against an already-reviewed decision).

Phase 4 rule: this copy lists only EP-01 to EP-10 and EP-19 to EP-21 (blueprint Section 11); EP-11
to EP-18 belong to `specs/005-review-delivery` and are not expected here. If a future sync ever
copies Phase 4 paths into this file too, `_phase3_paths` drops any path whose `summary` mentions
"Phase 4" so this test keeps comparing Phase 3 routes only.
"""

from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.domain.enums import ErrorCode
from tests.support.routes import api_routes

CONTRACT_PATH = Path(__file__).parent / "contracts" / "openapi_phase3.yaml"
PHASE4_CONTRACT_PATH = Path(__file__).parent / "contracts" / "openapi_phase4.yaml"

API_PREFIX = "/api/v1"


def _load_spec() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8")))


def _normalise(path: str) -> str:
    """Path params by position: `{id}` vs `{run_id}` compare equal, names don't matter."""
    segs = ("{}" if seg.startswith("{") and seg.endswith("}") else seg for seg in path.split("/"))
    return "/".join(segs)


_HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


def _phase3_paths(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """`{path: {method: operation}}` for every Phase 3 path (see module docstring)."""
    out: dict[str, dict[str, Any]] = {}
    for path, methods in spec["paths"].items():
        ops = {
            m: op
            for m, op in methods.items()
            if m in _HTTP_METHODS and "phase 4" not in str(op.get("summary", "")).lower()
        }
        if ops:
            out[path] = ops
    return out


@pytest.fixture
def app() -> FastAPI:
    from app.main import create_app

    return create_app()


def _phase4_paths() -> frozenset[str]:
    spec = cast(dict[str, Any], yaml.safe_load(PHASE4_CONTRACT_PATH.read_text(encoding="utf-8")))
    return frozenset(_normalise(API_PREFIX + p) for p in spec["paths"])


PHASE4_PATHS = _phase4_paths()
"""EP-11 to EP-18 (specs/005-review-delivery/contracts/openapi.yaml, copied to
`tests/contracts/openapi_phase4.yaml`): routed, but not in this Phase 3 copy (module docstring);
`test_api_phase4_contract.py` covers them."""


def test_paths_match_contract(app: FastAPI) -> None:
    """Every path+method in the YAML exists in the app's real routes and vice versa."""
    spec = _load_spec()
    expected = {
        (method.upper(), _normalise(API_PREFIX + path))
        for path, ops in _phase3_paths(spec).items()
        for method in ops
    }
    actual = {
        (method, _normalise(route.path))
        for route in api_routes(app)
        for method in route.methods
        if _normalise(route.path) not in PHASE4_PATHS
    }
    missing_in_app = expected - actual
    missing_in_contract = actual - expected
    assert not missing_in_app, f"declared in openapi.yaml but not routed: {missing_in_app}"
    assert not missing_in_contract, f"routed but not in openapi.yaml: {missing_in_contract}"


def _resolve_ref(spec: dict[str, Any], ref: str) -> dict[str, Any]:
    """Resolve a local `#/a/b/c` JSON pointer inside `spec`."""
    node: Any = spec
    for part in ref.removeprefix("#/").split("/"):
        node = node[part]
    return cast(dict[str, Any], node)


def _response_schema_ref(spec: dict[str, Any], response: dict[str, Any]) -> str | None:
    """The `$ref` of a response's `application/json` schema, resolving a `components/responses`
    indirection first (most Phase 3 non-2xx responses go through one)."""
    if "$ref" in response:
        response = _resolve_ref(spec, response["$ref"])
    schema = response.get("content", {}).get("application/json", {}).get("schema", {})
    ref = schema.get("$ref")
    return cast("str | None", ref)


_NON_ERROR_RESPONSES = {("/health", "get", "503")}
"""EP-01 503 intentionally reuses the `Health` schema, same body as 200 with `status: degraded`
(F-11.R7, research R10) — it is not an `ApiError` and is not expected to use `ErrorBody`."""


def test_error_responses_use_error_body(app: FastAPI) -> None:
    """Every non-2xx response declared in the YAML references the shared `ErrorBody` schema,
    except the documented exception in `_NON_ERROR_RESPONSES`."""
    spec = _load_spec()
    bad: list[str] = []
    for path, ops in _phase3_paths(spec).items():
        for method, op in ops.items():
            for status, response in op.get("responses", {}).items():
                if status.startswith("2") or (path, method, status) in _NON_ERROR_RESPONSES:
                    continue
                ref = _response_schema_ref(spec, response)
                if ref != "#/components/schemas/ErrorBody":
                    bad.append(f"{method.upper()} {path} {status} -> {ref}")
    assert not bad, "non-2xx responses not using ErrorBody:\n" + "\n".join(bad)


def _assert_error_shape(body: dict[str, Any]) -> None:
    assert set(body.keys()) == {"error"}
    error = body["error"]
    assert {"code", "message", "stage", "details"} <= set(error.keys())
    assert isinstance(error["code"], str)
    assert error["code"] in {c.value for c in ErrorCode}
    assert isinstance(error["message"], str)
    assert error["stage"] is None or isinstance(error["stage"], str)
    assert isinstance(error["details"], dict)


def test_real_401_matches_error_schema(client: TestClient) -> None:
    resp = client.get(f"{API_PREFIX}/runs")
    assert resp.status_code == 401
    _assert_error_shape(resp.json())
    assert resp.json()["error"]["code"] == "UNAUTHENTICATED"


def test_real_404_matches_error_schema(
    client: TestClient, tokens: dict[str, dict[str, str]]
) -> None:
    resp = client.get(f"{API_PREFIX}/runs/{uuid4()}", headers=tokens["controller"])
    assert resp.status_code == 404
    _assert_error_shape(resp.json())
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_real_400_matches_error_schema(
    client: TestClient, tokens: dict[str, dict[str, str]]
) -> None:
    resp = client.post(
        f"{API_PREFIX}/runs", json={"month": "2026-13"}, headers=tokens["controller"]
    )
    assert resp.status_code == 400
    _assert_error_shape(resp.json())
    assert resp.json()["error"]["code"] == "BAD_MONTH"


def test_real_409_matches_error_schema(
    client: TestClient, tokens: dict[str, dict[str, str]], memory_repo: Any
) -> None:
    from app.domain.enums import RunStatus
    from tests.factories import make_run

    done = memory_repo.create_run(make_run(status=RunStatus.FINALISED, run_key="f" * 64))
    resp = client.post(f"{API_PREFIX}/runs/{done.id}/uploads/confirm", headers=tokens["controller"])
    assert resp.status_code == 409
    _assert_error_shape(resp.json())
    assert resp.json()["error"]["code"] == "RUN_STATE_CONFLICT"
