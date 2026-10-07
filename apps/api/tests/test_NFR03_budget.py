"""NFR-03 (blueprint, research R7, R8): worst-case stage time stays under the Vercel budget."""

import json
from collections.abc import Sequence
from pathlib import Path

from app.core.config import API_DIR, Settings


def _worst_case_wait(attempts: int, waits: Sequence[float]) -> float:
    """Total backoff `services.runlog.retrying` spends across `attempts` calls: tenacity's
    `wait_chain` uses `waits[i]` before call `i + 2`, and the *last* wait value repeats for any
    further retry once `attempts - 1` exceeds `len(waits)` (research R8)."""
    needed = attempts - 1
    if needed <= 0:
        return 0
    if needed <= len(waits):
        return sum(waits[:needed])
    return sum(waits) + waits[-1] * (needed - len(waits))


def test_NFR03_extract_worst_case_under_200s() -> None:
    """3 attempts x 60 s timeout + (2 + 8) s backoff = 190 s < 200 s (research R7, R8)."""
    s = Settings(_env_file=None)
    worst_case = s.EXTRACT_ATTEMPTS * s.OPENAI_TIMEOUT_S + _worst_case_wait(
        s.EXTRACT_ATTEMPTS, s.EXTRACT_BACKOFF_S
    )
    assert worst_case < 200


def test_NFR03_telegram_worst_case_under_60s() -> None:
    """3 attempts x 10 s timeout + (1 + 4) s backoff = 35 s < 60 s (research R7, R8)."""
    s = Settings(_env_file=None)
    worst_case = s.TELEGRAM_ATTEMPTS * s.TELEGRAM_TIMEOUT_S + _worst_case_wait(
        s.TELEGRAM_ATTEMPTS, s.TELEGRAM_BACKOFF_S
    )
    assert worst_case < 60


def test_NFR03_vercel_max_duration_300() -> None:
    """Moved from test_api_settings.py (T044): the Vercel function limit covers the budget."""
    config = json.loads(Path(API_DIR, "vercel.json").read_text(encoding="utf-8"))
    assert all(f["maxDuration"] == 300 for f in config["functions"].values())
