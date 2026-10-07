"""006-web T002: the OpenAPI the web app generates its types from (apps/web/lib/api-types.ts)
exposes the flat EP-05 `RunView` (blueprint C25; 006 plan.md W3, W6, W7, W8) and a typed
`ReviewItem.context`."""

import json
from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient

from app.main import app
from app.services import review
from tests.support.pack_run import PackRun


def _schemas() -> dict[str, Any]:
    schemas: dict[str, Any] = app.openapi()["components"]["schemas"]
    return schemas


def _ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{name}"}


def test_T002_ep05_returns_flat_run_view() -> None:
    ok = app.openapi()["paths"]["/api/v1/runs/{id}"]["get"]["responses"]["200"]
    assert ok["content"]["application/json"]["schema"] == _ref("RunView")
    props = _schemas()["RunView"]["properties"]
    assert props["stage_progress"] == _ref("StageProgressView")
    assert props["open_review_items"]["type"] == "integer"
    assert props["headline"]["type"] == "string"
    assert props["exceptions"]["items"] == _ref("ExceptionItem")
    assert {"anyOf": [_ref("Notification"), {"type": "null"}]}.items() <= props["summary"].items()
    pages = _schemas()["PageProgress"]["properties"]
    assert set(pages) == {"page_no", "extracted", "cached"}


def test_T002_review_item_context_is_typed() -> None:
    context = _schemas()["ReviewItem"]["properties"]["context"]
    refs = {r["$ref"].rsplit("/", 1)[1] for r in context["anyOf"]}
    assert refs == {
        "CellContext",
        "RowInOneReadContext",
        "PageLabelMismatchContext",
        "UnknownMappingContext",
        "UnmatchedRowContext",
        "FlagAckContext",
    }


def test_T002_ep14_card_context_is_typed_with_view_fields() -> None:
    """EP-14 `ReviewCard.context`: the stored context plus the view fields (001 data-model
    "ReviewContext"), one schema per kind/reason, so SCR-06 reads page, row, table edges and
    the other sources without casting."""
    context = _schemas()["ReviewCard"]["properties"]["context"]
    refs = {r["$ref"].rsplit("/", 1)[1] for r in context["anyOf"]}
    assert refs == {
        "CellCardContext",
        "RowInOneReadCardContext",
        "PageLabelMismatchContext",
        "UnknownMappingCardContext",
        "UnmatchedRowContext",
        "FlagAckContext",
    }
    cell = _schemas()["CellCardContext"]["properties"]
    view = {"table_top", "table_bottom", "other_sources", "other_sources_label", "equals_sources"}
    assert {"page_no", "row_index", "candidates", "struck", "note"} | view == set(cell)
    row = _schemas()["RowInOneReadCardContext"]["properties"]
    assert {"page_no", "row_index", "read", "cells", "table_top", "table_bottom"} == set(row)
    mapping = _schemas()["UnknownMappingCardContext"]["properties"]
    assert {"source", "raw", "other_sources", "other_sources_label", "equals_sources"} == set(
        mapping
    )


def test_T002_ep14_typed_context_keeps_the_card_json(
    pack_run: PackRun, api_client_as: Callable[[str], TestClient]
) -> None:
    """The typed `context` serialises exactly the dict `review_card_context` builds, keys in
    evidence order (R3), for every card of the pack run."""
    resp = api_client_as("reviewer").get(f"/api/v1/runs/{pack_run.run_id}/review-items")
    assert resp.status_code == 200, resp.text
    built = review.list_cards(pack_run.repo, pack_run.run_id, None)
    got = [card["context"] for card in resp.json()]
    assert len(got) == len(built) > 0
    for context, (_, expected) in zip(got, built, strict=True):
        assert json.dumps(context) == json.dumps(expected)  # keys, order, int vs float
