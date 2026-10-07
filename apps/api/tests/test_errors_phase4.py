"""Phase 4 error classes: review/finalise failures in the shared error shape (data-model.md §1,
E1; F-08 AC4/AC5, FM-09, EP-17)."""

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import ConfirmRequired, InvalidReviewAction, ItemAlreadyResolved
from app.main import create_app


class _Item:
    """Stand-in for a `ReviewItem`-like object with a JSON-safe dump."""

    def __init__(self, **fields: Any) -> None:
        self._fields = fields

    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        return self._fields


def _app() -> FastAPI:
    app = create_app()

    @app.get("/t/invalid-review-action")
    def invalid_review_action() -> None:
        raise InvalidReviewAction("qty", "not a valid quantity")

    @app.get("/t/item-already-resolved")
    def item_already_resolved() -> None:
        raise ItemAlreadyResolved(_Item(id="item-1", status="RESOLVED", resolved_value="66"))

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
    assert err["details"]["item"] == {"id": "item-1", "status": "RESOLVED", "resolved_value": "66"}


def test_confirm_required_400(client: TestClient) -> None:
    resp = client.get("/t/confirm-required")
    assert resp.status_code == 400
    err = _error(resp)
    assert err["code"] == "CONFIRM_REQUIRED"
    assert err["stage"] == "finalise"
    assert err["details"] == {}
