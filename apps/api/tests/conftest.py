"""Shared fixtures. Pack files are resolved by path, not symlinked (research R15)."""

from datetime import datetime
from pathlib import Path

import pytest

API_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = API_DIR.parents[1]


@pytest.fixture(scope="session")
def pack_dir() -> Path:
    return REPO_ROOT / "data" / "kestrel-2026-09"


@pytest.fixture(scope="session")
def ground_truth_dir() -> Path:
    return API_DIR / "tests" / "ground_truth"


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
