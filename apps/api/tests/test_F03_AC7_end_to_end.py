"""F-03.AC7 end to end over HTTP (004 T042; spec US4 scenario 3, FM-04, FM-05, F-11.R2).

With `ALLOW_FAULTS=true`, `?fault=EXTRACT_TIMEOUT` / `EXTRACT_BAD_JSON` makes every model call of
an uncached page fail; the real Phase 2 retry loop runs (3 attempts per read, backoff skipped),
EP-09 answers 502 `UPSTREAM_FAILED` at stage `extract`, the run is `FAILED` at `extract` and
exactly one ops alert is sent; the controller chat is never messaged.

RETRY events: one per retry the reader reports (F-11.R2 "one RETRY per retry"). Both reads of
the page run in parallel, each making 3 attempts, so each read logs RETRY for attempts 2 and 3.
"""

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.memory_repo import MemoryRepo
from app.domain.enums import (
    AuditAction,
    NotificationKind,
    ReadVariant,
    RunEventStatus,
    RunStatus,
    Stage,
)
from app.pipeline.model_client import ReplayModelClient
from tests.fakes.api import FakeNotifier

API = "/api/v1"
Headers = dict[str, str]


def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
def replay(client: TestClient, cached_reads_dir: Path) -> Iterator[ReplayModelClient]:
    from app.core.deps import get_extract_sleep, get_model_client
    from tests.support.pack_pages import replay_client

    replay = replay_client(cached_reads_dir)
    overrides: dict[Any, Any] = client.app.dependency_overrides  # type: ignore[attr-defined]
    overrides[get_model_client] = lambda: replay
    overrides[get_extract_sleep] = lambda: _no_sleep
    yield replay


@pytest.fixture
def chats(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setenv("TELEGRAM_OPS_CHAT_ID", "ops-chat")
    monkeypatch.setenv("TELEGRAM_CONTROLLER_CHAT_ID", "controller-chat")
    get_settings.cache_clear()
    return {"ops": "ops-chat", "controller": "controller-chat"}


@pytest.fixture
def validated_run(
    faults_on: None,
    client: TestClient,
    tokens: dict[str, Headers],
    pack_upload: Callable[..., dict[str, Any]],
    replay: ReplayModelClient,
) -> str:
    """A pack run at `VALIDATED` with NO cached reads: both pages are uncached."""
    run_id: str = pack_upload()["run"]["id"]
    resp = client.post(f"{API}/runs/{run_id}/validate", headers=tokens["controller"])
    assert resp.status_code == 200, resp.text
    return run_id


def _assert_failed_after_retries(
    resp: Any,
    run_id: str,
    cause: str,
    memory_repo: MemoryRepo,
    notifier: FakeNotifier,
    chats: dict[str, str],
    replay: ReplayModelClient,
) -> None:
    attempts = get_settings().EXTRACT_ATTEMPTS
    assert attempts == 3
    assert resp.status_code == 502, resp.text
    error = resp.json()["error"]
    assert error["code"] == "UPSTREAM_FAILED"
    assert error["stage"] == "extract"
    assert error["details"]["service"] == "openai"
    assert error["details"]["attempts"] == attempts
    assert error["details"]["page_no"] == 1

    run = memory_repo.get_run(UUID(run_id))
    assert run.status is RunStatus.FAILED and run.failed_stage is Stage.EXTRACT
    assert run.error_message is not None and f"{cause} after 3 attempts" in run.error_message

    events = [e for e in memory_repo.list_run_events(UUID(run_id)) if e.stage is Stage.EXTRACT]
    assert events[0].status is RunEventStatus.FAILED  # newest first
    assert events[-1].status is RunEventStatus.STARTED
    retries = [e for e in events if e.status is RunEventStatus.RETRY]
    by_read = sorted((e.detail["variant"], e.detail["attempt"]) for e in retries)
    assert by_read == [(v.value, n) for v in ReadVariant for n in range(2, attempts + 1)]
    assert all(e.detail["page_no"] == 1 and e.detail["cause"] == cause for e in retries)
    assert replay.calls == 0  # the fault fails every call; the inner client is never used

    alerts = [
        n for n in memory_repo.list_notifications(UUID(run_id)) if n.kind is NotificationKind.ALERT
    ]
    assert len(alerts) == 1
    assert len(notifier.sent) == 1
    chat_id, text = notifier.sent[0]
    assert chat_id == chats["ops"]
    assert "Stage: extract (page 1)" in text
    assert all(chat != chats["controller"] for chat, _ in notifier.attempted)
    actions = [a.action for a in memory_repo.list_audit(run_id=UUID(run_id))]
    assert AuditAction.PAGE_EXTRACTED not in actions


def test_F03_AC7_end_to_end_timeout(
    chats: dict[str, str],
    client: TestClient,
    tokens: dict[str, Headers],
    memory_repo: MemoryRepo,
    notifier: FakeNotifier,
    replay: ReplayModelClient,
    validated_run: str,
) -> None:
    resp = client.post(
        f"{API}/runs/{validated_run}/pages/1/extract",
        params={"fault": "EXTRACT_TIMEOUT"},
        headers=tokens["controller"],
    )
    _assert_failed_after_retries(
        resp, validated_run, "timeout", memory_repo, notifier, chats, replay
    )


def test_F03_R5_attempts_and_backoff_come_from_settings(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    tokens: dict[str, Headers],
    memory_repo: MemoryRepo,
    validated_run: str,
) -> None:
    """T049 / NFR-03: EP-09 hands `EXTRACT_ATTEMPTS` and `EXTRACT_BACKOFF_S` to the reader."""
    from app.core.deps import get_extract_sleep

    monkeypatch.setenv("EXTRACT_ATTEMPTS", "2")
    monkeypatch.setenv("EXTRACT_BACKOFF_S", "[5]")
    get_settings.cache_clear()
    waits: list[float] = []
    overrides: dict[Any, Any] = client.app.dependency_overrides  # type: ignore[attr-defined]
    overrides[get_extract_sleep] = lambda: waits.append
    resp = client.post(
        f"{API}/runs/{validated_run}/pages/1/extract",
        params={"fault": "EXTRACT_TIMEOUT"},
        headers=tokens["controller"],
    )
    assert resp.status_code == 502
    assert resp.json()["error"]["details"]["attempts"] == 2
    assert waits == [5, 5]  # one wait per read (A and B)


def test_F03_extract_bad_json_counts_as_failed_attempt(
    chats: dict[str, str],
    client: TestClient,
    tokens: dict[str, Headers],
    memory_repo: MemoryRepo,
    notifier: FakeNotifier,
    replay: ReplayModelClient,
    validated_run: str,
) -> None:
    """FM-05: an answer that fails read validation is a failed attempt, retried like FM-04."""
    resp = client.post(
        f"{API}/runs/{validated_run}/pages/1/extract",
        params={"fault": "EXTRACT_BAD_JSON"},
        headers=tokens["controller"],
    )
    _assert_failed_after_retries(
        resp, validated_run, "invalid output", memory_repo, notifier, chats, replay
    )
