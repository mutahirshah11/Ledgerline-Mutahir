"""Shared fixtures. Pack files are resolved by path, not symlinked (research R15)."""

import os
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.core.db import Repo
    from app.core.memory_repo import MemoryRepo
    from app.core.storage import MemoryBlobStore
    from app.domain.models import Run
    from app.pipeline.engine import EngineInputs

API_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = API_DIR.parents[1]


@pytest.fixture(scope="session")
def pack_dir() -> Path:
    return REPO_ROOT / "data" / "kestrel-2026-09"


@pytest.fixture(scope="session")
def ground_truth_dir() -> Path:
    return API_DIR / "tests" / "ground_truth"


@pytest.fixture(scope="session")
def cached_reads_dir() -> Path:
    """The committed real reads of the pack pages (003 research R10)."""
    return REPO_ROOT / "supabase" / "seed" / "extractions"


def _live_openai_key() -> str | None:
    """OPENAI_API_KEY from the OS env, else apps/api/.env; empty counts as absent.

    Reads only this one key (never builds `Settings()`), so a malformed .env cannot break
    collection of the whole suite.
    """
    from dotenv import dotenv_values

    key = os.environ.get("OPENAI_API_KEY") or dotenv_values(API_DIR / ".env").get("OPENAI_API_KEY")
    return key or None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip `live` tests unless OPENAI_API_KEY is set (OS env or apps/api/.env).

    Live tests are also deselected by default (addopts); run them with `pytest -m live`.
    """
    if _live_openai_key() is not None:
        return
    skip_live = pytest.mark.skip(reason="needs OPENAI_API_KEY; skipped otherwise")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture(scope="session")
def live_settings() -> "Settings":
    """The real Settings (OS env + apps/api/.env) for `live` tests; skips without a key.

    Session-scoped, so it is built before the autouse `hermetic_settings` hides env vars
    and .env from each test. Live fixtures build `OpenAIModelClient` from this object, not
    from `get_settings()` (which is hermetic inside tests).
    """
    from app.core.config import Settings

    if _live_openai_key() is None:
        pytest.skip("needs OPENAI_API_KEY")
    try:
        settings = Settings()
    except Exception as exc:  # a malformed .env skips live tests, never errors them
        pytest.skip(f"Settings() failed: {type(exc).__name__}")
    if settings.OPENAI_API_KEY is None:
        pytest.skip("needs OPENAI_API_KEY")
    return settings


@pytest.fixture(autouse=True)
def hermetic_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Settings see no .env and no OS env vars, so local runs behave like CI."""
    from app.core.config import Settings, get_settings

    for name in Settings.model_fields:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Freeze app.core.clock at the scenario instant (2026-10-15 08:30 Asia/Karachi)."""
    from app.core import clock
    from app.core.config import Settings

    settings = Settings(
        _env_file=None,
        LEDGERLINE_NOW=datetime(2026, 10, 15, 8, 30, tzinfo=clock.KARACHI),
        LEDGERLINE_CLOCK_MODE="frozen",
        LEDGERLINE_NOW_ANCHOR=None,
    )
    monkeypatch.setattr(clock, "get_settings", lambda: settings)
    return clock.now()


# --- Repo and BlobStore (US4, contracts/repo-interface.md) -------------------------------

REPO_IMPLEMENTATIONS = ["memory_repo"]
"""Fixture names of every Repo implementation; Phase 3 appends "supabase_repo"."""


@pytest.fixture
def memory_repo() -> "MemoryRepo":
    from app.core.memory_repo import MemoryRepo

    return MemoryRepo()


@pytest.fixture(params=REPO_IMPLEMENTATIONS)
def repo(request: pytest.FixtureRequest) -> "Repo":
    """Every Repo implementation; the shared contract suite runs once per entry."""
    impl: Repo = request.getfixturevalue(request.param)
    return impl


@pytest.fixture
def memory_blobs() -> "MemoryBlobStore":
    """MemoryBlobStore whose signed-URL expiry is computed from a fixed instant."""
    from app.core.storage import MemoryBlobStore
    from tests.factories import T0

    return MemoryBlobStore(now=lambda: T0)


# --- Engine inputs (Phase 1, tests/support/pack.py) --------------------------------------

from tests.support.pack import PACK_RUN_ID as PACK_RUN_ID  # noqa: E402


@pytest.fixture
def extracted_run(memory_repo: "MemoryRepo", frozen_clock: datetime) -> "Run":
    """The 2026-09 pack run in `memory_repo`, walked CREATED → … → EXTRACTED."""
    from app.domain.enums import RunStatus
    from tests.factories import make_run

    run = memory_repo.create_run(
        make_run(id=PACK_RUN_ID, scenario_now=frozen_clock, created_at=frozen_clock)
    )
    path = [RunStatus.UPLOADED, RunStatus.VALIDATED, RunStatus.EXTRACTING, RunStatus.EXTRACTED]
    for to in path:
        run = memory_repo.transition_run(run.id, to, expected={run.status})
    return run


@pytest.fixture
def engine_inputs(extracted_run: "Run", pack_dir: Path, ground_truth_dir: Path) -> "EngineInputs":
    """Pack inputs for `extracted_run`, no decisions. Fresh per test (models are shallow-frozen)."""
    from tests.support.pack import build_engine_inputs

    return build_engine_inputs(pack_dir, ground_truth_dir, extracted_run.id)
