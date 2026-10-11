"""Delete a run (007-ship QA request): CONTROLLER only, confirm required, any status, the
audit log keeps a RUN_DELETED entry, and the same pack can be finalised again afterwards."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.errors import NotFound
from app.domain.enums import AuditAction, RunStatus
from tests.fakes.api import FakeNotifier
from tests.support.pack_run import PackRun

API = "/api/v1"


@pytest.fixture(autouse=True)
def chats(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CONTROLLER_CHAT_ID", "controller-chat")
    monkeypatch.setenv("TELEGRAM_OPS_CHAT_ID", "ops-chat")
    get_settings.cache_clear()


def test_delete_finalised_run_removes_it_and_keeps_audit(
    pack_run: PackRun,
    api_client_as: Callable[[str], TestClient],
    fake_notifier: FakeNotifier,
) -> None:
    client = api_client_as("controller")
    fin = client.post(f"{API}/runs/{pack_run.run_id}/finalise", json={"confirm": True})
    assert fin.status_code == 200, fin.text
    run_key = pack_run.repo.get_run(pack_run.run_id).run_key
    assert pack_run.repo.get_run(pack_run.run_id).status is RunStatus.FINALISED

    resp = client.post(f"{API}/runs/{pack_run.run_id}/delete", json={"confirm": True})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"id": str(pack_run.run_id), "deleted": True}
    with pytest.raises(NotFound):
        pack_run.repo.get_run(pack_run.run_id)
    assert client.get(f"{API}/runs/{pack_run.run_id}").status_code == 404
    assert all(r.id != pack_run.run_id for r in pack_run.repo.list_runs())
    assert pack_run.repo.list_review_items(pack_run.run_id) == []
    assert pack_run.repo.list_source_files(pack_run.run_id) == []
    assert run_key is not None and pack_run.repo.find_finalised_run_by_key(run_key) is None
    actions = [a.action for a in pack_run.repo.list_audit(run_id=pack_run.run_id)]
    assert AuditAction.RUN_FINALISED in actions
    assert actions.count(AuditAction.RUN_DELETED) == 1  # newest first, every earlier entry kept


def test_delete_requires_confirm(
    pack_run: PackRun, api_client_as: Callable[[str], TestClient]
) -> None:
    client = api_client_as("controller")
    for body in (None, {}, {"confirm": False}):
        resp = client.post(f"{API}/runs/{pack_run.run_id}/delete", json=body)
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["code"] == "CONFIRM_REQUIRED"
    assert pack_run.repo.get_run(pack_run.run_id).id == pack_run.run_id


@pytest.mark.parametrize("role", ["reviewer", "ops"])
def test_only_controller_deletes(
    role: str, pack_run: PackRun, api_client_as: Callable[[str], TestClient]
) -> None:
    resp = api_client_as(role).post(f"{API}/runs/{pack_run.run_id}/delete", json={"confirm": True})
    assert resp.status_code == 403, resp.text
    assert pack_run.repo.get_run(pack_run.run_id).id == pack_run.run_id


def test_delete_unknown_run_is_404(api_client_as: Callable[[str], TestClient]) -> None:
    resp = api_client_as("controller").post(
        f"{API}/runs/00000000-0000-4000-8000-000000000000/delete", json={"confirm": True}
    )
    assert resp.status_code == 404, resp.text
