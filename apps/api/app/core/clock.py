"""Scenario clock (F-11.R1, contracts/clock.md): the only reader of the system clock."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from app.core.config import get_settings

KARACHI = ZoneInfo("Asia/Karachi")


def _real_utc_now() -> datetime:
    """The single place the system clock is read; tests monkeypatch it."""
    return datetime.now(UTC)


def now() -> datetime:
    """Current scenario time in Asia/Karachi: real, frozen, or scenario plus real elapsed."""
    settings = get_settings()
    scenario = settings.LEDGERLINE_NOW
    if scenario is None:
        instant = _real_utc_now()
    elif settings.clock_mode == "offset":
        assert settings.LEDGERLINE_NOW_ANCHOR is not None  # guaranteed by Settings validation
        instant = scenario + (_real_utc_now() - settings.LEDGERLINE_NOW_ANCHOR)
    else:
        instant = scenario
    return instant.astimezone(KARACHI)


def today() -> date:
    """Scenario date in Asia/Karachi."""
    return now().date()
