"""Phase 4 error classes: review/finalise failures in the shared error shape (data-model.md §1,
E1; F-08 AC4/AC5, FM-09, EP-17)."""

import datetime as dt
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import ConfirmRequired, InvalidReviewAction, ItemAlreadyResolved
from app.domain.enums import ReviewStatus
from app.main import create_app
from tests.factories import make_review

ITEM = make_review(
    UUID("7f3c0000-0000-4000-8000-000000000001"),
    "cell:REG-p1-r01:qty",
    status=ReviewStatus.RESOLVED,
    resolved_value="66",
    resolved_by="controller@kestrel.example",
    resolved_at=dt.datetime(2026, 10, 15, 8, 30, tzinfo=dt.timezone(dt.timedelta(hours=5))),
)
"""The already-decided item the racing reviewer gets back (FM-09)."""


def _app() -> FastAPI:
    app = create_app()

    @app.get("/t/invalid-review-action")
    def invalid_review_action() -> None:
        raise InvalidReviewAction("qty", "not a valid quantity")

    @app.get("/t/item-already-resolved")
    def item_already_resolved() -> None:
        raise ItemAlreadyResolved(ITEM)

    @app.get("/t/confirm-required")
    def confirm_required() -> None:
        raise ConfirmRequired()

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


def test_invalid_review_action_422_carries_field_and_reason(client: TestClient) -> None:
    resp = client.get("/t/invalid-review-action")
    assert resp.status_code == 422
    err = _error(resp)
    assert err["code"] == "INVALID_REVIEW_ACTION"
    assert err["stage"] == "review"
    assert err["details"] == {"field": "qty", "reason": "not a valid quantity"}


def test_item_already_resolved_409_carries_item(client: TestClient) -> None:
    resp = client.get("/t/item-already-resolved")
    assert resp.status_code == 409
    err = _error(resp)
    assert err["code"] == "ITEM_ALREADY_RESOLVED"
    assert err["stage"] == "review"
    assert err["details"]["item"] == ITEM.model_dump(mode="json")
    assert err["details"]["item"]["resolved_value"] == "66"


def test_confirm_required_400(client: TestClient) -> None:
    resp = client.get("/t/confirm-required")
    assert resp.status_code == 400
    err = _error(resp)
    assert err["code"] == "CONFIRM_REQUIRED"
    assert err["stage"] == "finalise"
    assert err["details"] == {}
