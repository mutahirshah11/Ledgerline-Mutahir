"""006-web read cost (spec.md Clarifications, Session 2026-10-07): the estimated model cost of
the new pages before reading (EP-07, EP-05) and the actual cost of what this run read (EP-05).

Prices come from settings (`EXTRACT_PRICE_INPUT_PER_MTOK`, `EXTRACT_PRICE_OUTPUT_PER_MTOK`),
the per-page token averages from `EXTRACT_EST_*_TOKENS_PER_PAGE` (Phase 2 measurement); USD is
a Decimal to 0.001, a string in JSON, and None when either price is unset.
"""

from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.core.memory_repo import MemoryRepo
from app.domain.models import Extraction
from app.pipeline.cost import estimate_read_cost, read_cost_of
from app.pipeline.model_client import ReplayModelClient
from tests.conftest import store_cached_extractions
from tests.factories import make_extraction

API = "/api/v1"
Headers = dict[str, str]


@pytest.fixture
def prices(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The Phase 2 prices: US$0.20 input and US$1.20 output per 1M tokens."""
    monkeypatch.setenv("EXTRACT_PRICE_INPUT_PER_MTOK", "0.20")
    monkeypatch.setenv("EXTRACT_PRICE_OUTPUT_PER_MTOK", "1.20")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _settings(**over: Any) -> Settings:
    base: dict[str, Any] = {
        "EXTRACT_PRICE_INPUT_PER_MTOK": Decimal("0.20"),
        "EXTRACT_PRICE_OUTPUT_PER_MTOK": Decimal("1.20"),
    }
    return Settings(**(base | over))


# --- pure functions -------------------------------------------------------------------------


def test_read_cost_settings_default_to_the_phase2_measurement() -> None:
    settings = get_settings()
    assert settings.EXTRACT_EST_INPUT_TOKENS_PER_PAGE == 9500
    assert settings.EXTRACT_EST_OUTPUT_TOKENS_PER_PAGE == 8000
    assert settings.EXTRACT_PRICE_INPUT_PER_MTOK is None  # never hard-coded


def test_estimate_is_new_pages_times_the_page_average_times_the_prices() -> None:
    # 2 x (9500 x 0.20 + 8000 x 1.20) / 1M = 0.023
    assert estimate_read_cost(2, _settings()) == Decimal("0.023")
    assert estimate_read_cost(1, _settings()) == Decimal("0.012")  # 0.0115 rounds half up
    assert estimate_read_cost(0, _settings()) == Decimal("0.000")
    other = _settings(
        EXTRACT_EST_INPUT_TOKENS_PER_PAGE=1_000_000,
        EXTRACT_EST_OUTPUT_TOKENS_PER_PAGE=500_000,
        EXTRACT_PRICE_INPUT_PER_MTOK=Decimal("1"),
        EXTRACT_PRICE_OUTPUT_PER_MTOK=Decimal("2"),
    )
    assert estimate_read_cost(3, other) == Decimal("6.000")


def test_cost_is_none_while_either_price_is_unset() -> None:
    no_out = _settings(EXTRACT_PRICE_OUTPUT_PER_MTOK=None)
    no_in = _settings(EXTRACT_PRICE_INPUT_PER_MTOK=None)
    for settings in (no_out, no_in):
        assert estimate_read_cost(2, settings) is None
        assert read_cost_of([make_extraction()], settings) is None


def test_actual_is_the_stored_tokens_times_the_prices() -> None:
    reads = [
        make_extraction(input_tokens=4700, output_tokens=4100),
        make_extraction(input_tokens=4800, output_tokens=3900),
    ]
    # (9500 x 0.20 + 8000 x 1.20) / 1M = 0.0115 -> 0.012
    assert read_cost_of(reads, _settings()) == Decimal("0.012")
    assert read_cost_of([make_extraction(input_tokens=2500, output_tokens=0)], _settings()) == (
        Decimal("0.001")  # 0.0005 rounds half up
    )
    assert read_cost_of([], _settings()) == Decimal("0.000")


# --- EP-07 and EP-05 -------------------------------------------------------------------------


@pytest.fixture
def replay(client: TestClient, cached_reads_dir: Path) -> ReplayModelClient:
    """EP-09's model client: replays committed reads, no network (as test_api_reads.py)."""
    from app.core.deps import get_extract_sleep, get_model_client
    from tests.support.pack_pages import replay_client

    replay = replay_client(cached_reads_dir)
    overrides: dict[Any, Any] = client.app.dependency_overrides  # type: ignore[attr-defined]
    overrides[get_model_client] = lambda: replay
    overrides[get_extract_sleep] = lambda: lambda _s: None
    return replay


def _extract(client: TestClient, headers: Headers, run_id: str) -> None:
    resp = client.post(f"{API}/runs/{run_id}/validate", headers=headers)
    assert resp.status_code == 200, resp.text
    for page in (1, 2):
        resp = client.post(f"{API}/runs/{run_id}/pages/{page}/extract", headers=headers)
        assert resp.status_code == 200, resp.text


def _read_cost(client: TestClient, headers: Headers, run_id: str) -> dict[str, Any]:
    resp = client.get(f"{API}/runs/{run_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()["read_cost"]
    return body


def test_ep07_estimates_the_new_pages(
    prices: None, pack_upload: Callable[..., dict[str, Any]]
) -> None:
    body = pack_upload()
    assert (body["new_pages"], body["known_pages"]) == ([1, 2], [])
    assert body["read_cost_estimate_usd"] == "0.023"


def test_ep07_estimate_is_null_without_prices(pack_upload: Callable[..., dict[str, Any]]) -> None:
    assert pack_upload()["read_cost_estimate_usd"] is None


def test_ep05_read_cost_before_and_after_reading_new_pages(
    prices: None,
    client: TestClient,
    tokens: dict[str, Headers],
    pack_upload: Callable[..., dict[str, Any]],
    replay: ReplayModelClient,
    memory_repo: MemoryRepo,
) -> None:
    headers = tokens["controller"]
    run_id: str = pack_upload()["run"]["id"]
    assert _read_cost(client, headers, run_id) == {
        "new_pages": 2,
        "estimate_usd": "0.023",
        "actual_usd": "0.000",
        "input_tokens": 0,
        "output_tokens": 0,
    }
    _extract(client, headers, run_id)
    settings = get_settings()
    shas = [f.sha256 for f in memory_repo.list_source_files(memory_repo.list_runs(1)[0].id)]
    reads = memory_repo.list_extractions(shas, settings.EXTRACT_MODEL, settings.PROMPT_VERSION)
    assert len(reads) == 4
    cost = _read_cost(client, headers, run_id)
    assert cost == {
        "new_pages": 2,
        "estimate_usd": "0.023",
        "actual_usd": str(read_cost_of(reads, settings)),
        "input_tokens": sum(r.input_tokens for r in reads),
        "output_tokens": sum(r.output_tokens for r in reads),
    }
    assert cost["input_tokens"] > 0 and cost["output_tokens"] > 0


def test_ep05_cached_pages_cost_nothing(
    prices: None,
    client: TestClient,
    tokens: dict[str, Headers],
    pack_upload: Callable[..., dict[str, Any]],
    replay: ReplayModelClient,
    memory_repo: MemoryRepo,
    cached_extraction_records: list[Extraction],
) -> None:
    """Pages read before this run come from the cache: no new pages, US$0.000 actual."""
    store_cached_extractions(memory_repo, cached_extraction_records)
    headers = tokens["controller"]
    run_id: str = pack_upload()["run"]["id"]
    _extract(client, headers, run_id)
    assert _read_cost(client, headers, run_id) == {
        "new_pages": 0,
        "estimate_usd": "0.000",
        "actual_usd": "0.000",
        "input_tokens": 0,
        "output_tokens": 0,
    }


def test_ep05_read_cost_is_null_usd_without_prices(
    client: TestClient, tokens: dict[str, Headers], pack_upload: Callable[..., dict[str, Any]]
) -> None:
    headers = tokens["controller"]
    run_id: str = pack_upload()["run"]["id"]
    assert _read_cost(client, headers, run_id) == {
        "new_pages": 2,
        "estimate_usd": None,
        "actual_usd": None,
        "input_tokens": 0,
        "output_tokens": 0,
    }
