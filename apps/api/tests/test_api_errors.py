"""One error shape for every failure (FR-026, blueprint Section 11, C17, data-model.md §1)."""

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.core.errors import ApiError, NotFound, StateConflict, UniqueViolation, UpstreamFailed
from app.domain.enums import ErrorCode, RunStatus, Stage
from app.main import create_app


class _Body(BaseModel):
    month: str


class _Current:
    status = RunStatus.FINALISED
    failed_stage = None


def _app() -> FastAPI:
    app = create_app()

    @app.get("/t/not-found")
    def not_found() -> None:
        raise NotFound("run", "r1")

    @app.get("/t/conflict")
    def conflict() -> None:
        raise StateConflict(_Current())

    @app.get("/t/unique")
    def unique() -> None:
        raise UniqueViolation("runs_month_key")

    @app.get("/t/upstream")
    def upstream() -> None:
        raise UpstreamFailed("openai", "timeout", stage="extract", page_no=2)

    @app.post("/t/body")
    def body(payload: _Body) -> None:
        return None

    @app.get("/t/api-error")
    def api_error() -> None:
        raise ApiError(ErrorCode.BAD_MONTH, 400, "Month is not valid", details={"month": "x"})

    @app.get("/t/boom")
    def boom() -> None:
        raise RuntimeError("secret internals at line 42")

    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_app(), raise_server_exceptions=False)


def _error(resp: Any) -> dict[str, Any]:
    body = resp.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "stage", "details"}
    err: dict[str, Any] = body["error"]
    return err


def test_error_shape_for_not_found(client: TestClient) -> None:
    resp = client.get("/t/not-found")
    assert resp.status_code == 404
    err = _error(resp)
    assert err["code"] == "NOT_FOUND"
    assert err["stage"] is None
    assert isinstance(err["details"], dict)


def test_state_conflict_maps_409_with_status(client: TestClient) -> None:
    resp = client.get("/t/conflict")
    assert resp.status_code == 409
    err = _error(resp)
    assert err["code"] == "RUN_STATE_CONFLICT"
    assert err["details"] == {"status": "FINALISED", "failed_stage": None}


def test_unique_violation_maps_409(client: TestClient) -> None:
    resp = client.get("/t/unique")
    assert resp.status_code == 409
    assert _error(resp)["code"] == "RUN_STATE_CONFLICT"


def test_upstream_failed_maps_502(client: TestClient) -> None:
    resp = client.get("/t/upstream")
    assert resp.status_code == 502
    err = _error(resp)
    assert err["code"] == "UPSTREAM_FAILED"
    assert err["stage"] == Stage.EXTRACT.value
    assert err["details"] == {"service": "openai", "page_no": 2}


def test_request_validation_maps_400_bad_request(client: TestClient) -> None:
    resp = client.post("/t/body", json={"wrong": 1})
    assert resp.status_code == 400
    err = _error(resp)
    assert err["code"] == "BAD_REQUEST"
    fields = err["details"]["fields"]
    assert fields and set(fields[0]) == {"loc", "msg"}
    assert fields[0]["loc"] == ["body", "month"]


def test_api_error_uses_its_own_status(client: TestClient) -> None:
    resp = client.get("/t/api-error")
    assert resp.status_code == 400
    err = _error(resp)
    assert (err["code"], err["message"], err["details"]) == (
        "BAD_MONTH",
        "Month is not valid",
        {"month": "x"},
    )


def test_unknown_route_uses_error_shape(client: TestClient) -> None:
    resp = client.get("/api/v1/nope")
    assert resp.status_code == 404
    assert _error(resp)["code"] == "NOT_FOUND"


def test_unexpected_exception_500_internal_error_no_trace_in_body(client: TestClient) -> None:
    resp = client.get("/t/boom")
    assert resp.status_code == 500
    assert _error(resp) == {
        "code": "INTERNAL_ERROR",
        "message": "Unexpected error",
        "stage": None,
        "details": {},
    }
    assert "secret internals" not in resp.text
    assert "Traceback" not in resp.text


def test_api_error_details_default_empty() -> None:
    err = ApiError(ErrorCode.NOT_ALLOWED, 403, "No access", stage=Stage.REVIEW)
    assert (err.code, err.http_status, err.stage, err.details) == (
        ErrorCode.NOT_ALLOWED,
        403,
        Stage.REVIEW,
        {},
    )
