"""F-11 AC5 audit trail: one `REVIEW_RESOLVED` row per decision, one `RUN_FINALISED` and one
`SUMMARY_SENT` on finalise (005 T062; data-model.md §8), read back via EP-20.
"""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.domain.enums import AuditAction, AuditChannel
from tests.fakes.api import FakeNotifier
from tests.support.pack_run import PackRun

API = "/api/v1"
Client = Callable[[str], TestClient]
CONTROLLER_CHAT = "controller-chat"


@pytest.fixture(autouse=True)
def chats(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CONTROLLER_CHAT_ID", CONTROLLER_CHAT)
    monkeypatch.setenv("TELEGRAM_OPS_CHAT_ID", "ops-chat")
    get_settings.cache_clear()


QTY_4419 = "CELL:REG-p2-r05:qty"
QTY_4428 = "CELL:REG-p2-r14:qty"
GAP_4429 = "FLAG:SEQUENCE_GAP:4429"
BACKDATED_4430 = "FLAG:BACKDATED_ENTRY:ERP-0028"

ACCEPT = {"action": "ACCEPT_READ"}
ENTER_250 = {"action": "ENTER_VALUE", "value": "250"}
ACK = {"action": "ACKNOWLEDGE"}


def _item_id(pack: PackRun, key: str) -> str:
    (item,) = [i for i in pack.repo.list_review_items(pack.run_id) if i.item_key == key]
    return str(item.id)


def _resolve(client: TestClient, pack: PackRun, key: str, body: dict[str, Any]) -> None:
    item_id = _item_id(pack, key)
    resp = client.post(f"{API}/review-items/{item_id}/resolve", json=body)
    assert resp.status_code == 200, resp.text


def _audit(client: TestClient, pack: PackRun) -> list[dict[str, Any]]:
    resp = client.get(f"{API}/runs/{pack.run_id}/audit")
    assert resp.status_code == 200, resp.text
    entries: list[dict[str, Any]] = resp.json()
    return entries


def test_F11_AC5_one_audit_row_per_decision_with_before_after(
    pack_run: PackRun, api_client_as: Client
) -> None:
    reviewer = api_client_as("reviewer")
    _resolve(reviewer, pack_run, QTY_4419, ACCEPT)
    _resolve(reviewer, pack_run, QTY_4428, ENTER_250)
    _resolve(reviewer, pack_run, GAP_4429, ACK)
    _resolve(reviewer, pack_run, BACKDATED_4430, ACK)

    entries = _audit(reviewer, pack_run)
    resolved = [e for e in entries if e["action"] == AuditAction.REVIEW_RESOLVED.value]
    assert len(resolved) == 4  # exactly one per decision

    item_ids = {_item_id(pack_run, k) for k in (QTY_4419, QTY_4428, GAP_4429, BACKDATED_4430)}
    seen_items = set()
    for entry in resolved:
        assert entry["entity"] == "review_item"
        assert entry["entity_id"] in item_ids
        seen_items.add(entry["entity_id"])
        assert entry["before"]  # non-empty
        assert entry["after"]  # non-empty
        assert entry["actor"] == "reviewer@kestrel.example"
        assert entry["at"] == pack_run.run.scenario_now.isoformat()
        assert entry["run_id"] == str(pack_run.run_id)
    assert seen_items == item_ids

    (entry_4428,) = [e for e in resolved if e["entity_id"] == _item_id(pack_run, QTY_4428)]
    assert entry_4428["after"]["equals_sources"] == ["ERP", "SHEET"]


def test_F11_finalise_and_summary_audited(
    pack_run: PackRun, api_client_as: Client, fake_notifier: FakeNotifier
) -> None:
    controller = api_client_as("controller")
    resp = controller.post(f"{API}/runs/{pack_run.run_id}/finalise", json={"confirm": True})
    assert resp.status_code == 200, resp.text

    entries = _audit(controller, pack_run)
    finalised = [e for e in entries if e["action"] == AuditAction.RUN_FINALISED.value]
    sent = [e for e in entries if e["action"] == AuditAction.SUMMARY_SENT.value]
    assert len(finalised) == 1
    assert len(sent) == 1
    for entry in (*finalised, *sent):
        assert entry["run_id"] == str(pack_run.run_id)
    assert sent[0]["channel"] == AuditChannel.TELEGRAM.value
