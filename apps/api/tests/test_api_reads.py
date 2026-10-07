"""EP-19, EP-20, EP-21 read endpoints (004 T055; spec US6 scenarios 3-5, FR-034).

The pack is driven to AWAITING_REVIEW through the real API (upload, validate, extract with
cached reads, reconcile) and then read back: run events newest first with durations, audit
entries newest first with an actor filter, and the normalisation log holding the XLS-r18
`DATE_TEXT_PARSED` entry (F-04, blueprint GE-04).
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.models import Extraction
from app.pipeline.model_client import ReplayModelClient
from tests.conftest import store_cached_extractions

API = "/api/v1"
Headers = dict[str, str]


@pytest.fixture
def replay(client: TestClient, cached_reads_dir: Path) -> ReplayModelClient:
    """EP-09's model client: replays committed reads, no network (as test_api_stages.py)."""
    from app.core.deps import get_extract_sleep, get_model_client
    from tests.support.pack_pages import replay_client

    replay = replay_client(cached_reads_dir)
    overrides: dict[Any, Any] = client.app.dependency_overrides  # type: ignore[attr-defined]
    overrides[get_model_client] = lambda: replay
    overrides[get_extract_sleep] = lambda: lambda _s: None
    return replay


@pytest.fixture
def cached(memory_repo: Any, cached_extraction_records: list[Extraction]) -> int:
    return store_cached_extractions(memory_repo, cached_extraction_records)


@pytest.fixture
def reconciled_run(
    client: TestClient,
    tokens: dict[str, Headers],
    pack_upload: Callable[..., dict[str, Any]],
    replay: ReplayModelClient,
    cached: int,
) -> str:
    """A pack run taken from upload through EP-10 (AWAITING_REVIEW), via the real API."""
    headers = tokens["controller"]
    run_id: str = pack_upload()["run"]["id"]
    resp = client.post(f"{API}/runs/{run_id}/validate", headers=headers)
    assert resp.status_code == 200, resp.text
    for page in (1, 2):
        resp = client.post(f"{API}/runs/{run_id}/pages/{page}/extract", headers=headers)
        assert resp.status_code == 200, resp.text
    resp = client.post(f"{API}/runs/{run_id}/reconcile", headers=headers)
    assert resp.status_code == 200, resp.text
    return run_id


def _error(resp: Any) -> dict[str, Any]:
    body: dict[str, Any] = resp.json()["error"]
    return body


# --- EP-19 -------------------------------------------------------------------------------


def test_EP19_events_newest_first_with_durations(
    client: TestClient, tokens: dict[str, Headers], reconciled_run: str
) -> None:
    resp = client.get(f"{API}/runs/{reconciled_run}/events", headers=tokens["controller"])
    assert resp.status_code == 200, resp.text
    events = resp.json()
    assert events  # at least STARTED/OK per stage
    for e in events:
        assert set(e) == {"id", "stage", "status", "at", "duration_ms", "detail"}
    ats = [e["at"] for e in events]
    assert ats == sorted(ats, reverse=True)  # newest first
    stages = {e["stage"] for e in events}
    assert stages >= {"upload", "validate", "extract", "reconcile"}
    ok_events = [e for e in events if e["status"] == "OK"]
    assert ok_events and all(e["duration_ms"] is not None for e in ok_events)
    extract_ok = [e for e in ok_events if e["stage"] == "extract"]
    assert sorted(e["detail"]["page_no"] for e in extract_ok) == [1, 2]


# --- EP-20 -------------------------------------------------------------------------------


def test_EP20_audit_newest_first_and_actor_filter(
    client: TestClient, tokens: dict[str, Headers], reconciled_run: str
) -> None:
    headers = tokens["controller"]
    resp = client.get(f"{API}/runs/{reconciled_run}/audit", headers=headers)
    assert resp.status_code == 200, resp.text
    entries = resp.json()
    assert entries
    for e in entries:
        assert set(e) == {
            "id",
            "at",
            "actor",
            "action",
            "entity",
            "entity_id",
            "before",
            "after",
            "channel",
        }
    ats = [e["at"] for e in entries]
    assert ats == sorted(ats, reverse=True)
    actions = {e["action"] for e in entries}
    assert actions >= {"RUN_CREATED", "FILE_UPLOADED", "RUN_VALIDATED", "PAGE_EXTRACTED"}

    by_controller = client.get(
        f"{API}/runs/{reconciled_run}/audit",
        params={"actor": "controller@kestrel.example"},
        headers=headers,
    ).json()
    assert by_controller and all(e["actor"] == "controller@kestrel.example" for e in by_controller)
    assert "RECONCILED" not in {e["action"] for e in by_controller}  # written by `system`

    by_system = client.get(
        f"{API}/runs/{reconciled_run}/audit", params={"actor": "system"}, headers=headers
    ).json()
    assert by_system and all(e["actor"] == "system" for e in by_system)
    assert {e["action"] for e in by_system} == {"RECONCILED"}


# --- EP-21 -------------------------------------------------------------------------------


def test_EP21_norm_log_contains_xls_r18_date_text_parsed(
    client: TestClient, tokens: dict[str, Headers], reconciled_run: str
) -> None:
    headers = tokens["controller"]
    resp = client.get(f"{API}/runs/{reconciled_run}/normalisation-log", headers=headers)
    assert resp.status_code == 200, resp.text
    entries = resp.json()
    assert entries
    for e in entries:
        assert set(e) >= {"source_row_id", "field", "code", "before", "after"}
    matches = [e for e in entries if e["source_row_id"] == "XLS-r18" and e["field"] == "date"]
    assert matches == [
        {
            "source_row_id": "XLS-r18",
            "field": "date",
            "code": "DATE_TEXT_PARSED",
            "before": "9/14/2026",
            "after": "2026-09-14",
        }
    ]

    filtered = client.get(
        f"{API}/runs/{reconciled_run}/normalisation-log",
        params={"source_row_id": "XLS-r18"},
        headers=headers,
    ).json()
    assert filtered and all(e["source_row_id"] == "XLS-r18" for e in filtered)
    assert matches[0] in filtered


# --- unknown run ---------------------------------------------------------------------------


def test_reads_unknown_run_404(client: TestClient, tokens: dict[str, Headers]) -> None:
    headers = tokens["controller"]
    unknown = uuid4()
    for path in ("events", "audit", "normalisation-log"):
        resp = client.get(f"{API}/runs/{unknown}/{path}", headers=headers)
        assert resp.status_code == 404, resp.text
        assert _error(resp)["code"] == "NOT_FOUND"
