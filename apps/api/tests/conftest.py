"""Shared fixtures. Pack files are resolved by path, not symlinked (research R15)."""

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
