"""006-web carry-overs from 004 (NFR-02): EP-04 and EP-05 read the store a bounded number of
times, whatever the number of runs listed or pages in the run."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from tests.support.pack_run import PackRun
from tests.test_F08_hardening import _count_calls

API = "/api/v1"
Client = Callable[[str], TestClient]


def test_NFR02_ep05_reads_all_page_extractions_in_one_call(
    pack_run: PackRun, api_client_as: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before: `get_extraction` per page and read (2 pages x 2 reads = 4 calls); after: one
    `list_extractions` for every page of the run."""
    client = api_client_as("reviewer")
    calls = _count_calls(pack_run.repo, monkeypatch)
    resp = client.get(f"{API}/runs/{pack_run.run_id}")
    assert resp.status_code == 200, resp.text
    assert calls.get("get_extraction", 0) == 0, sorted(calls.items())
    assert calls["list_extractions"] == 1
    pages = resp.json()["stage_progress"]["pages"]
    assert [(p["page_no"], p["extracted"]) for p in pages] == [(1, True), (2, True)]
