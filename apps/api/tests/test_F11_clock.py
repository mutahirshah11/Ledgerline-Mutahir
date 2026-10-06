"""F-11.R1 scenario clock (contracts/clock.md): modes, validation, and the system-clock lint ban."""

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.core import clock
from app.core.config import Settings, get_settings

API_DIR = Path(__file__).resolve().parents[1]
SCENARIO = "2026-10-15T08:30:00+05:00"
KARACHI = ZoneInfo("Asia/Karachi")


def _settings(**values: object) -> Settings:
    """Build Settings from explicit values only, never from the developer's .env."""
    clock_values: dict[str, object] = {
        "LEDGERLINE_NOW": None,
        "LEDGERLINE_CLOCK_MODE": None,
        "LEDGERLINE_NOW_ANCHOR": None,
    }
    clock_values.update(values)
    return Settings(_env_file=None, **clock_values)  # type: ignore[arg-type]


def _use(monkeypatch: pytest.MonkeyPatch, settings: Settings, real_utc: datetime) -> None:
    monkeypatch.setattr(clock, "get_settings", lambda: settings)
    monkeypatch.setattr(clock, "_real_utc_now", lambda: real_utc)


def test_F11_R1_frozen_returns_fixed_instant(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(LEDGERLINE_NOW=SCENARIO, LEDGERLINE_CLOCK_MODE="frozen")
    _use(monkeypatch, settings, datetime(2030, 1, 1, tzinfo=UTC))
    first = clock.now()
    _use(monkeypatch, settings, datetime(2030, 1, 1, 0, 5, tzinfo=UTC))
    second = clock.now()

    expected = datetime(2026, 10, 15, 8, 30, tzinfo=KARACHI)
    assert first == second == expected
    assert first.tzinfo == clock.KARACHI == KARACHI
    assert clock.today().isoformat() == "2026-10-15"


def test_F11_R1_frozen_is_default_mode_when_now_set(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(LEDGERLINE_NOW="2026-10-15T03:30:00+00:00")
    _use(monkeypatch, settings, datetime(2030, 1, 1, tzinfo=UTC))
    result = clock.now()
    assert result.utcoffset() == timedelta(hours=5)
    assert (result.hour, result.minute) == (8, 30)


def test_F11_R1_offset_adds_elapsed(monkeypatch: pytest.MonkeyPatch) -> None:
    anchor = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    settings = _settings(
        LEDGERLINE_NOW=SCENARIO,
        LEDGERLINE_CLOCK_MODE="offset",
        LEDGERLINE_NOW_ANCHOR=anchor.isoformat(),
    )
    _use(monkeypatch, settings, anchor + timedelta(seconds=90))

    result = clock.now()
    assert result == datetime(2026, 10, 15, 8, 31, 30, tzinfo=KARACHI)
    assert result.tzinfo == KARACHI


def test_F11_R1_unset_uses_real_time_karachi(monkeypatch: pytest.MonkeyPatch) -> None:
    real = datetime(2026, 10, 6, 20, 0, tzinfo=UTC)
    _use(monkeypatch, _settings(), real)

    result = clock.now()
    assert result == real
    assert result.tzinfo == KARACHI
    assert (result.day, result.hour) == (7, 1)
    assert clock.today().isoformat() == "2026-10-07"


def test_F11_R1_real_utc_now_is_aware() -> None:
    assert clock._real_utc_now().utcoffset() == timedelta(0)


def test_F11_R1_rejects_naive_now() -> None:
    with pytest.raises(ValidationError, match="LEDGERLINE_NOW"):
        _settings(LEDGERLINE_NOW="2026-10-15T08:30:00")
    with pytest.raises(ValidationError, match="LEDGERLINE_NOW"):
        _settings(LEDGERLINE_NOW="not-a-time")


def test_F11_R1_offset_requires_anchor() -> None:
    with pytest.raises(ValidationError, match="LEDGERLINE_NOW_ANCHOR"):
        _settings(LEDGERLINE_NOW=SCENARIO, LEDGERLINE_CLOCK_MODE="offset")
    with pytest.raises(ValidationError, match="LEDGERLINE_NOW_ANCHOR"):
        _settings(
            LEDGERLINE_NOW=SCENARIO,
            LEDGERLINE_CLOCK_MODE="offset",
            LEDGERLINE_NOW_ANCHOR="2026-10-06T12:00:00",
        )
    with pytest.raises(ValidationError, match="LEDGERLINE_NOW_ANCHOR"):
        _settings(
            LEDGERLINE_NOW=SCENARIO,
            LEDGERLINE_CLOCK_MODE="offset",
            LEDGERLINE_NOW_ANCHOR="yesterday",
        )


def test_F11_R1_rejects_unknown_mode() -> None:
    with pytest.raises(ValidationError, match="LEDGERLINE_CLOCK_MODE"):
        _settings(LEDGERLINE_NOW=SCENARIO, LEDGERLINE_CLOCK_MODE="rewind")


def test_F11_R1_settings_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEDGERLINE_NOW", SCENARIO)
    monkeypatch.setenv("LEDGERLINE_CLOCK_MODE", "offset")
    monkeypatch.setenv("LEDGERLINE_NOW_ANCHOR", "")
    with pytest.raises(ValidationError, match="LEDGERLINE_NOW_ANCHOR"):
        Settings(_env_file=None)


def test_F11_R1_lint_bans_system_clock(tmp_path: Path) -> None:
    planted = tmp_path / "planted_clock.py"
    planted.write_text(
        '"""Planted violation."""\n\nfrom datetime import datetime\n\nSTAMP = datetime.now()\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "--config",
            str(API_DIR / "pyproject.toml"),
            str(planted),
        ],
        cwd=API_DIR,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "TID251" in result.stdout
    assert planted.name in result.stdout


def test_F11_R1_frozen_clock_fixture(frozen_clock: datetime) -> None:
    assert clock.now() == frozen_clock == datetime(2026, 10, 15, 8, 30, tzinfo=KARACHI)


def test_F11_R1_settings_hermetic_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """No developer .env or OS env var leaks into tests: the clock is real time unless frozen."""
    real = datetime(2026, 10, 6, 20, 0, tzinfo=UTC)
    monkeypatch.setattr(clock, "_real_utc_now", lambda: real)

    settings = get_settings()
    assert settings.LEDGERLINE_NOW is None
    assert settings.EXTRACT_PRICE_INPUT_PER_MTOK is None
    assert clock.now() == real


def test_F11_R1_offset_without_anchor_raises_not_asserts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The invariant survives `python -O`: an explicit RuntimeError, not an assert."""
    unchecked = SimpleNamespace(
        LEDGERLINE_NOW=datetime(2026, 10, 15, 3, 30, tzinfo=UTC),
        clock_mode="offset",
        LEDGERLINE_NOW_ANCHOR=None,
    )
    monkeypatch.setattr(clock, "get_settings", lambda: unchecked)
    with pytest.raises(RuntimeError, match="LEDGERLINE_NOW_ANCHOR"):
        clock.now()
