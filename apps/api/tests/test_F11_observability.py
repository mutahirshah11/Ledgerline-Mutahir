"""F-11 observability (blueprint Section 11, F-11.R1 to R7). 004 T051.

Alerts and health (US5, F-11.AC2, F-11.R4, F-11.R5, F-11.R7) land here first; another agent
later adds the F-11.AC1 (scenario-clock timestamps) and F-11.AC4 (no secrets in logs) tests
to this same module (T057) once logging and the full pipeline wiring exist. The stage-driven
alert tests (`test_F11_AC2_*`, `test_F11_R4_retry_same_minute_no_second_alert`,
`test_F11_R5_alert_failure_marks_run_and_health`) need EP-09/EP-10 and stay open for US4/US5;
only the health-level view of F-11.R5 is tested here.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.db import get_repo
from app.core.errors import UpstreamFailed
from app.core.memory_repo import MemoryRepo
from app.core.storage import MemoryBlobStore, get_blobstore
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


def _telegram_settings() -> dict[str, str]:
    """Env values that make every F-11.R7 setting check pass, built at runtime (never a
    secret-looking literal)."""
    return {
        "OPENAI_API_KEY": _fake("oai"),
        "TELEGRAM_BOT_TOKEN": _fake("tgb"),
        "TELEGRAM_OPS_CHAT_ID": "ops-chat-" + "9k2" * 3,
        "TELEGRAM_CONTROLLER_CHAT_ID": "controller-chat-" + "7h4" * 3,
    }


@contextmanager
def _health_client(
    monkeypatch: pytest.MonkeyPatch, repo: Any, blobstore: Any, **env: str
) -> Iterator[TestClient]:
    """A fresh `/api/v1/health` app over `repo`/`blobstore`, with `env` applied before
    `Settings` is built: `create_app()` wires dependencies at build time, so settings have to
    be set before the client exists, not inside the test body after it (004 review)."""
    from app.main import create_app

    for name, value in env.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides[get_repo] = lambda: repo
    app.dependency_overrides[get_blobstore] = lambda: blobstore
    with TestClient(app) as client:
        yield client


class DownRepo(MemoryRepo):
    """`MemoryRepo` with `ping()` false (FM-14); `get_latest_notification` must never be
    called while the db is down (004 review item 2) — it raises if it is."""

    def __init__(self) -> None:
        super().__init__()
        self.notification_reads = 0

    def ping(self) -> bool:
        return False

    def get_latest_notification(self, kind: NotificationKind) -> Any:
        self.notification_reads += 1
        raise UpstreamFailed("supabase", "down")


class DownReportsBlobs(MemoryBlobStore):
    """`MemoryBlobStore` whose `reports` bucket is unreachable; `inputs` stays ok."""

    def bucket_ok(self, bucket: str) -> bool:
        return bucket == "inputs"


# --- T051: health (F-11.R7, FM-14, contracts/alerts.md) -----------------------------------


def test_F11_R7_health_no_auth_reports_checks(
    monkeypatch: pytest.MonkeyPatch, memory_repo: MemoryRepo, memory_blobs: MemoryBlobStore
) -> None:
    with _health_client(monkeypatch, memory_repo, memory_blobs, **_telegram_settings()) as client:
        resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == HEALTH_FIELDS
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert body["storage"] == "ok"
    assert body["openai_key_set"] is True
    assert body["telegram_set"] is True
    assert body["telegram"] == "ok"
    assert body["scenario_now"]


def test_F11_R7_health_never_returns_setting_values(
    monkeypatch: pytest.MonkeyPatch, memory_repo: MemoryRepo, memory_blobs: MemoryBlobStore
) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://distinct-health-test.supabase.co")
    env = _telegram_settings() | {"SUPABASE_SERVICE_ROLE_KEY": _fake("srk")}
    with _health_client(monkeypatch, memory_repo, memory_blobs, **env) as client:
        resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    settings = get_settings()
    assert len(settings.secret_values()) == 3
    text = resp.text
    for value in settings.secret_values():
        assert value not in text
    assert settings.SUPABASE_URL not in text
    assert settings.TELEGRAM_OPS_CHAT_ID not in text
    assert settings.TELEGRAM_CONTROLLER_CHAT_ID not in text


def test_F11_R7_health_missing_openai_key_503(
    monkeypatch: pytest.MonkeyPatch, memory_repo: MemoryRepo, memory_blobs: MemoryBlobStore
) -> None:
    env = _telegram_settings()
    del env["OPENAI_API_KEY"]
    with _health_client(monkeypatch, memory_repo, memory_blobs, **env) as client:
        resp = client.get("/api/v1/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["openai_key_set"] is False
    assert body["db"] == "ok"
    assert body["storage"] == "ok"


def test_F11_R7_health_unset_when_telegram_missing(
    monkeypatch: pytest.MonkeyPatch, memory_repo: MemoryRepo, memory_blobs: MemoryBlobStore
) -> None:
    with _health_client(
        monkeypatch, memory_repo, memory_blobs, OPENAI_API_KEY=_fake("oai")
    ) as client:
        resp = client.get("/api/v1/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["telegram_set"] is False
    assert body["telegram"] == "unset"
    assert body["status"] == "degraded"


def test_F11_R7_health_telegram_failing_503(
    monkeypatch: pytest.MonkeyPatch, memory_repo: MemoryRepo, memory_blobs: MemoryBlobStore
) -> None:
    with _health_client(monkeypatch, memory_repo, memory_blobs, **_telegram_settings()) as client:
        run = memory_repo.create_run(make_run())
        memory_repo.insert_notification(
            make_notification(
                run.id,
                kind=NotificationKind.ALERT,
                status=NotificationStatus.FAILED,
                updated_at=run.created_at,
            )
        )
        resp = client.get("/api/v1/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["telegram"] == "failing"
    assert body["status"] == "degraded"


def test_F11_R7_health_degraded_503_when_db_down(
    monkeypatch: pytest.MonkeyPatch, memory_blobs: MemoryBlobStore
) -> None:
    down = DownRepo()
    with _health_client(monkeypatch, down, memory_blobs, **_telegram_settings()) as client:
        resp = client.get("/api/v1/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["db"] == "failing"
    assert body["storage"] == "ok"
    assert body["telegram"] == "failing"  # can't read the alert state while the db is down
    assert down.notification_reads == 0  # never called (004 review item 2)


def test_F11_R7_health_degraded_503_when_storage_down(
    monkeypatch: pytest.MonkeyPatch, memory_repo: MemoryRepo
) -> None:
    down_blobs = DownReportsBlobs()
    with _health_client(monkeypatch, memory_repo, down_blobs, **_telegram_settings()) as client:
        resp = client.get("/api/v1/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["db"] == "ok"
    assert body["storage"] == "failing"


def test_F11_R5_health_failing_after_failed_alert(
    monkeypatch: pytest.MonkeyPatch, memory_repo: MemoryRepo, memory_blobs: MemoryBlobStore
) -> None:
    with _health_client(monkeypatch, memory_repo, memory_blobs, **_telegram_settings()) as client:
        run = memory_repo.create_run(make_run())
        memory_repo.insert_notification(
            make_notification(
                run.id,
                kind=NotificationKind.ALERT,
                status=NotificationStatus.FAILED,
                updated_at=run.created_at,
            )
        )
        resp = client.get("/api/v1/health")
    assert resp.json()["telegram"] == "failing"
    assert resp.status_code == 503


@pytest.mark.parametrize("status", [NotificationStatus.SENDING, NotificationStatus.PENDING])
def test_F11_R5_health_failing_when_alert_stuck_sending(
    status: NotificationStatus,
    monkeypatch: pytest.MonkeyPatch,
    memory_repo: MemoryRepo,
    memory_blobs: MemoryBlobStore,
    frozen_clock: Any,
    move_clock: Any,
) -> None:
    """A `SENDING` or `PENDING` `ALERT` row older than 5 minutes is `failing` (contracts/alerts.md:
    a send that never finished starting is as unresolved as one that never finished sending)."""
    with _health_client(monkeypatch, memory_repo, memory_blobs, **_telegram_settings()) as client:
        run = memory_repo.create_run(make_run(created_at=frozen_clock, scenario_now=frozen_clock))
        memory_repo.insert_notification(
            make_notification(
                run.id, kind=NotificationKind.ALERT, status=status, updated_at=frozen_clock
            )
        )
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        assert resp.json()["telegram"] == "ok"  # younger than 5 minutes

        move_clock(frozen_clock + timedelta(minutes=6))
        resp = client.get("/api/v1/health")
    assert resp.status_code == 503
    assert resp.json()["telegram"] == "failing"
