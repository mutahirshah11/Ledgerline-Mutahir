"""F-11 observability (blueprint Section 11, F-11.R1 to R7). 004 T051.

Alerts and health (US5, F-11.AC2, F-11.R4, F-11.R5, F-11.R7) land here first; another agent
later adds the F-11.AC1 (scenario-clock timestamps) and F-11.AC4 (no secrets in logs) tests
to this same module (T057) once logging and the full pipeline wiring exist.
"""

from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.domain.enums import NotificationKind, NotificationStatus
from tests.factories import make_notification, make_run

HEALTH_FIELDS = {
    "status",
    "db",
    "storage",
    "openai_key_set",
    "telegram_set",
    "telegram",
    "scenario_now",
}


def _fake(prefix: str) -> str:
    """A distinctive fake secret built at runtime (no secret-looking literal in the source)."""
    return prefix + "-" + "Zq7" * 8


def _health_app() -> FastAPI:
    """A fresh app wired to the shared memory repo/blobstore (no `client` fixture override),
    so a test can seed `shared_memory_repo()` directly before calling `/api/v1/health`."""
    from app.main import create_app

    return create_app()


# --- T051: health (F-11.R7, FM-14) --------------------------------------------------------


def test_F11_R7_health_no_auth_reports_checks() -> None:
    client = TestClient(_health_app())
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == HEALTH_FIELDS
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert body["storage"] == "ok"
    assert body["openai_key_set"] is False
    assert body["telegram_set"] is False
    assert body["telegram"] == "unset"
    assert body["scenario_now"]


def test_F11_R7_health_never_returns_setting_values(monkeypatch: pytest.MonkeyPatch) -> None:
    secrets = {
        "SUPABASE_SERVICE_ROLE_KEY": _fake("srk"),
        "OPENAI_API_KEY": _fake("oai"),
        "TELEGRAM_BOT_TOKEN": _fake("tgb"),
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("TELEGRAM_OPS_CHAT_ID", "ops-chat")
    monkeypatch.setenv("TELEGRAM_CONTROLLER_CHAT_ID", "controller-chat")
    get_settings.cache_clear()

    client = TestClient(_health_app())
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json()["openai_key_set"] is True
    assert resp.json()["telegram_set"] is True
    text = resp.text
    for value in get_settings().secret_values():
        assert value not in text


def test_F11_R7_health_degraded_503_when_db_down() -> None:
    from app.core.db import get_repo
    from app.core.storage import get_blobstore, shared_memory_blobstore

    class DownRepo:
        def ping(self) -> bool:
            return False

        def get_latest_notification(self, kind: NotificationKind) -> Any:
            return None

    app = _health_app()
    app.dependency_overrides[get_repo] = lambda: DownRepo()
    app.dependency_overrides[get_blobstore] = lambda: shared_memory_blobstore()
    client = TestClient(app)
    resp = client.get("/api/v1/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["db"] == "failing"
    assert body["storage"] == "ok"


def test_F11_R7_health_unset_when_telegram_missing() -> None:
    client = TestClient(_health_app())
    body = client.get("/api/v1/health").json()
    assert body["telegram_set"] is False
    assert body["telegram"] == "unset"


def _seed_alert(status: NotificationStatus, *, updated_at: Any, frozen_clock: Any) -> None:
    from app.core.db import shared_memory_repo

    repo = shared_memory_repo()
    run = repo.create_run(make_run(created_at=frozen_clock, scenario_now=frozen_clock))
    repo.insert_notification(
        make_notification(
            run.id,
            kind=NotificationKind.ALERT,
            status=status,
            updated_at=updated_at,
        )
    )


def test_F11_R5_health_failing_after_failed_alert(
    frozen_clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _fake("tgb"))
    monkeypatch.setenv("TELEGRAM_OPS_CHAT_ID", "ops-chat")
    monkeypatch.setenv("TELEGRAM_CONTROLLER_CHAT_ID", "controller-chat")
    get_settings.cache_clear()
    _seed_alert(NotificationStatus.FAILED, updated_at=frozen_clock, frozen_clock=frozen_clock)

    client = TestClient(_health_app())
    body = client.get("/api/v1/health").json()
    assert body["telegram"] == "failing"


def test_F11_R5_health_failing_when_alert_stuck_sending(
    frozen_clock: Any, move_clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _fake("tgb"))
    monkeypatch.setenv("TELEGRAM_OPS_CHAT_ID", "ops-chat")
    monkeypatch.setenv("TELEGRAM_CONTROLLER_CHAT_ID", "controller-chat")
    get_settings.cache_clear()
    _seed_alert(NotificationStatus.SENDING, updated_at=frozen_clock, frozen_clock=frozen_clock)

    client = TestClient(_health_app())
    body = client.get("/api/v1/health").json()
    assert body["telegram"] == "ok"  # younger than 5 minutes

    move_clock(frozen_clock + timedelta(minutes=6))
    body = client.get("/api/v1/health").json()
    assert body["telegram"] == "failing"  # stuck SENDING > 5 min (contracts/alerts.md)
