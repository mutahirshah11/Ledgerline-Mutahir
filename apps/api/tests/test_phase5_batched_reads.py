"""006-web carry-overs from 004 (NFR-02): EP-04 and EP-05 read the store a bounded number of
times, whatever the number of runs listed or pages in the run."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from app.services import stages
from tests.factories import make_run
from tests.support.pack_run import PackRun
from tests.test_F08_hardening import _count_calls
from tests.test_phase5_repo import failed_proof

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


EP04_CALLS = 3
"""`list_runs`, `count_outcomes`, `list_proof_statuses`; before: 1 + 3 per listed run
(`list_source_rows` and `list_groups` for the counts, `get_proof`)."""


def test_NFR02_ep04_reads_a_constant_number_of_times(
    pack_run: PackRun, api_client_as: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = pack_run.repo
    extra = [repo.create_run(make_run()) for _ in range(5)]
    repo.save_proof(failed_proof(extra[0].id))
    client = api_client_as("reviewer")
    calls = _count_calls(repo, monkeypatch)
    resp = client.get(f"{API}/runs", params={"limit": 50})
    assert resp.status_code == 200, resp.text
    calls.pop("get_allowed_user", None)
    assert sum(calls.values()) == EP04_CALLS, sorted(calls.items())
    listed = {r["id"]: r for r in resp.json()}
    assert len(listed) == 6
    pack = listed[str(pack_run.run_id)]
    expected = stages.run_outcome_counts(repo, pack_run.run_id)
    assert pack["counts"] == {o.value: n for o, n in expected.items()}
    assert pack["proof_status"] == "PASS"
    assert listed[str(extra[0].id)]["proof_status"] == "FAIL"
    assert listed[str(extra[1].id)]["proof_status"] is None
    assert set(listed[str(extra[1].id)]["counts"].values()) == {0}
