"""F-07 AC6: a failing proof blocks finalise and nothing reaches the controller (005 T048)."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.domain.enums import RunStatus
from tests.fakes.api import FakeNotifier
from tests.support.pack_run import PackRun

API = "/api/v1"
CONTROLLER_CHAT = "controller-chat"


@pytest.fixture(autouse=True)
def chats(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CONTROLLER_CHAT_ID", CONTROLLER_CHAT)
    monkeypatch.setenv("TELEGRAM_OPS_CHAT_ID", "ops-chat")
    get_settings.cache_clear()


def test_F07_AC6_finalise_409_and_no_telegram_when_proof_fails(
    faults_on: None,
    pack_run: PackRun,
    api_client_as: Callable[[str], TestClient],
    fake_notifier: FakeNotifier,
) -> None:
    client = api_client_as("controller")
    run_url = f"{API}/runs/{pack_run.run_id}"
    dropped = client.post(f"{run_url}/reconcile", params={"fault": "PROOF_DROP_ROW"})
    assert dropped.status_code == 200, dropped.text
    assert dropped.json()["proof_status"] == "FAIL"
    resp = client.post(f"{run_url}/finalise", json={"confirm": True})
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "PROOF_NOT_PASSED"
    assert pack_run.repo.get_run(pack_run.run_id).status is not RunStatus.FINALISED
    assert fake_notifier.texts(CONTROLLER_CHAT) == []
    assert fake_notifier.documents_to(CONTROLLER_CHAT) == []
