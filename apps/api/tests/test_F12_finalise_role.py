"""F-12 AC3: only the controller finalises; reviewer and ops get 403 (005 T048, F-12.R4)."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from tests.fakes.api import FakeNotifier
from tests.support.pack_run import PackRun

API = "/api/v1"


@pytest.fixture(autouse=True)
def chats(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CONTROLLER_CHAT_ID", "controller-chat")
    monkeypatch.setenv("TELEGRAM_OPS_CHAT_ID", "ops-chat")
    get_settings.cache_clear()


@pytest.mark.parametrize("role", ["reviewer", "ops"])
def test_F12_AC3_reviewer_cannot_finalise(
    role: str,
    pack_run: PackRun,
    api_client_as: Callable[[str], TestClient],
    fake_notifier: FakeNotifier,
) -> None:
    before = pack_run.repo.get_run(pack_run.run_id)
    resp = api_client_as(role).post(
        f"{API}/runs/{pack_run.run_id}/finalise", json={"confirm": True}
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "NOT_ALLOWED"
    assert pack_run.repo.get_run(pack_run.run_id) == before
    assert fake_notifier.sent == [] and fake_notifier.documents == []
    assert pack_run.repo.list_notifications(pack_run.run_id) == []
