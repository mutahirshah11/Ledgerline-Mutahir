"""Shared fixtures. Pack files are resolved by path, not symlinked (research R15)."""

import os
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from app.core.config import Settings
    from app.core.db import Repo
    from app.core.memory_repo import MemoryRepo
    from app.core.storage import MemoryBlobStore
    from app.domain.models import Run
    from app.pipeline.engine import EngineInputs
    from app.pipeline.model_client import ReplayModelClient
    from tests.fakes.api import FakeJwksFetcher, FakeNotifier
    from tests.support.pack_pages import PackPages

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
    """Settings see no .env and no OS env vars, so local runs behave like CI; the backend is
    `memory` and the process-wide memory stores start empty in every test."""
    from app.core.config import get_settings
    from app.core.db import shared_memory_repo
    from app.core.storage import shared_memory_blobstore
    from tests.support.pack_pages import make_settings_hermetic

    make_settings_hermetic(monkeypatch)
    monkeypatch.setenv("REPO_BACKEND", "memory")
    shared_memory_repo.cache_clear()
    shared_memory_blobstore.cache_clear()
    yield
    get_settings.cache_clear()
    shared_memory_repo.cache_clear()
    shared_memory_blobstore.cache_clear()


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


# --- Pack pages through the extract service (003 T026, research R10, R13) ----------------

PACK_PAGE_SOURCES = [
    pytest.param("cache", id="cache"),
    pytest.param("live", id="live", marks=pytest.mark.live),
]
"""`cache`: committed real reads (fails when none, T042); `live`: the model (skips without key)."""


@pytest.fixture(scope="session")
def pack_images(pack_dir: Path) -> dict[int, bytes]:
    """The two register page images of the pack, by page number (the bytes `pack_pages`
    extracts; test_F03_pack checks each source read exactly these)."""
    from scripts.seed_cache import page_image
    from tests.support.pack import PAGES

    return {p: page_image(pack_dir, p) for p in PAGES}


@pytest.fixture
def replay_client(cached_reads_dir: Path) -> "ReplayModelClient":
    """Replays the committed reads for the committed EXTRACT_MODEL and PROMPT_VERSION, for a
    test that reads committed pages directly (T026); `pack_pages` builds its own."""
    from tests.support.pack_pages import replay_client as build

    return build(cached_reads_dir)


@pytest.fixture(scope="module", params=PACK_PAGE_SOURCES)
def pack_pages(
    request: pytest.FixtureRequest, pack_dir: Path, cached_reads_dir: Path
) -> "PackPages":
    """`extract_page` results for pack pages 1 and 2 (`pack_pages[1]`, `pack_pages[2]`) plus
    the reads behind them, extracted once per module and source.

    `cache` loads `supabase/seed/extractions/` into a MemoryRepo (`load_cached_extractions`)
    and replays it; it fails (T042) when no read exists for the current model and prompt version.
    It runs with settings made hermetic here (module fixtures run before the autouse
    `hermetic_settings`), so the OS env and .env change neither the files matched nor the clock.
    `live` reads afresh with `OpenAIModelClient` built from `live_settings` into a MemoryRepo,
    writing nothing to disk; its retries back off for real.
    """
    from app.core.memory_repo import MemoryRepo
    from app.pipeline.model_client import OpenAIModelClient
    from tests.support.extracted import require_committed_reads
    from tests.support.pack_pages import cached_pack, extract_pack, hermetic_settings_context

    if request.param == "cache":
        with hermetic_settings_context():
            require_committed_reads(cached_reads_dir)  # T042: fail, never skip
            return cached_pack(cached_reads_dir, pack_dir)
    settings: Settings = request.getfixturevalue("live_settings")
    assert settings.OPENAI_API_KEY is not None
    client = OpenAIModelClient(
        api_key=settings.OPENAI_API_KEY.get_secret_value(), model=settings.EXTRACT_MODEL
    )
    return extract_pack(
        "live", repo=MemoryRepo(), client=client, settings=settings, pack_dir=pack_dir
    )


# --- API client (004 T010, contracts/auth.md) --------------------------------------------

SEEDED_USERS = {
    "controller": ("controller@kestrel.example", "Controller (Demo)", "CONTROLLER"),
    "reviewer": ("reviewer@kestrel.example", "Reviewer (Demo)", "REVIEWER"),
    "ops": ("ops@kestrel.example", "Ops (Demo)", "OPS"),
}
"""The allowlist of `supabase/seed/allowed_users.sql` (C13), seeded into the client's repo."""
STRANGER_EMAIL = "stranger@kestrel.example"
"""Signs valid tokens but is not on the allowlist (F-12 AC2)."""


@pytest.fixture
def api_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """`SUPABASE_URL` for API tests, so `TEST_ISS` is the expected issuer."""
    from app.core.config import get_settings
    from tests.fakes.api import TEST_SUPABASE_URL

    monkeypatch.setenv("SUPABASE_URL", TEST_SUPABASE_URL)
    get_settings.cache_clear()


@pytest.fixture
def faults_on(monkeypatch: pytest.MonkeyPatch, api_settings: None) -> None:
    """`ALLOW_FAULTS=true` (Section 25). Request it before `client` when the app's docs routes
    matter; `active_fault` reads settings per request either way."""
    from app.core.config import get_settings

    monkeypatch.setenv("ALLOW_FAULTS", "true")
    get_settings.cache_clear()


@pytest.fixture
def jwks_fetcher() -> "FakeJwksFetcher":
    """The JWKS source behind `client`; set `.fail` or swap `.keys` to simulate Supabase."""
    from tests.fakes.api import FakeJwksFetcher

    return FakeJwksFetcher()


@pytest.fixture
def notifier() -> "FakeNotifier":
    from tests.fakes.api import FakeNotifier

    return FakeNotifier()


@pytest.fixture
def client(
    api_settings: None,
    frozen_clock: datetime,
    memory_repo: "MemoryRepo",
    memory_blobs: "MemoryBlobStore",
    jwks_fetcher: "FakeJwksFetcher",
    notifier: "FakeNotifier",
) -> Iterator["TestClient"]:
    """`TestClient(create_app())` over `memory_repo`, `memory_blobs`, `notifier` and the fake
    JWKS, with the three seeded users on the allowlist. `pack_upload` runs EP-03 to EP-07 for
    the pack."""
    from fastapi.testclient import TestClient

    from app.core.auth import JwksCache, get_jwks_cache
    from app.core.db import get_repo
    from app.core.deps import get_notifier
    from app.core.storage import get_blobstore
    from app.domain.enums import Role
    from app.domain.models import AllowedUser
    from app.main import create_app

    for email, name, role in SEEDED_USERS.values():
        memory_repo.upsert_allowed_user(
            AllowedUser(email=email, display_name=name, role=Role(role))
        )
    jwks = JwksCache(jwks_fetcher)
    app = create_app()
    app.dependency_overrides[get_repo] = lambda: memory_repo
    app.dependency_overrides[get_blobstore] = lambda: memory_blobs
    app.dependency_overrides[get_notifier] = lambda: notifier
    app.dependency_overrides[get_jwks_cache] = lambda: jwks
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def tokens() -> dict[str, dict[str, str]]:
    """`Authorization` headers for `controller`, `reviewer`, `ops` and `stranger`."""
    from tests.fakes.api import make_token

    emails = {who: user[0] for who, user in SEEDED_USERS.items()} | {"stranger": STRANGER_EMAIL}
    return {who: {"Authorization": f"Bearer {make_token(e)}"} for who, e in emails.items()}


PACK_FILES = {
    "t4_register_page1.jpg": "REGISTER",
    "t4_register_page2.jpg": "REGISTER",
    "t4_erp_grn.csv": "ERP",
    "t4_store_sheet.xlsx": "SHEET",
}
"""The pack's four files and their sources, in upload order (pages 1 and 2 first)."""


@pytest.fixture(scope="session")
def pack_files(pack_dir: Path) -> dict[str, bytes]:
    """The bytes of the four pack files, read once, by file name."""
    return {name: (pack_dir / name).read_bytes() for name in PACK_FILES}


@pytest.fixture(scope="session")
def pack_declared(pack_files: dict[str, bytes]) -> list[dict[str, Any]]:
    """The EP-06 `files` list for the pack (sha256 computed as the browser does, F-01.R2)."""
    import hashlib

    return [
        {
            "source": PACK_FILES[name],
            "filename": name,
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        for name, data in pack_files.items()
    ]


@pytest.fixture
def pack_upload(
    client: "TestClient",
    tokens: dict[str, dict[str, str]],
    memory_blobs: "MemoryBlobStore",
    pack_files: dict[str, bytes],
    pack_declared: list[dict[str, Any]],
) -> Callable[..., dict[str, Any]]:
    """`pack_upload(month="2026-09")`: EP-03 → EP-06 → blob put → EP-07 as the controller for the
    pack (004 T010); returns the EP-07 body (`run`, `new_pages`, `known_pages`)."""
    from app.core.storage import INPUTS_BUCKET

    def upload(month: str = "2026-09") -> dict[str, Any]:
        headers = tokens["controller"]
        created = client.post("/api/v1/runs", json={"month": month}, headers=headers)
        assert created.status_code == 201, created.text
        run_id = created.json()["id"]
        slots = client.post(
            f"/api/v1/runs/{run_id}/uploads", json={"files": pack_declared}, headers=headers
        )
        assert slots.status_code == 200, slots.text
        for slot in slots.json()["uploads"]:
            data = pack_files[slot["filename"]]
            memory_blobs.put(INPUTS_BUCKET, slot["storage_path"], data, "application/octet-stream")
        confirmed = client.post(f"/api/v1/runs/{run_id}/uploads/confirm", headers=headers)
        assert confirmed.status_code == 200, confirmed.text
        body: dict[str, Any] = confirmed.json()
        return body

    return upload
