"""Phase 3 settings (004 data-model.md §5, NFR-03, Section 10)."""

import json
from pathlib import Path

from app.core.config import API_DIR, Settings


def test_phase3_setting_defaults() -> None:
    s = Settings(_env_file=None)
    assert (s.OPENAI_TIMEOUT_S, s.EXTRACT_ATTEMPTS, s.EXTRACT_BACKOFF_S) == (60, 3, (2, 8))
    assert (s.TELEGRAM_ATTEMPTS, s.TELEGRAM_BACKOFF_S, s.TELEGRAM_TIMEOUT_S) == (3, (1, 4), 10)
    assert (s.UPLOAD_WINDOW_S, s.DOWNLOAD_URL_S, s.JWKS_CACHE_S) == (600, 300, 600)
    assert s.REPO_BACKEND == "supabase"


def test_cors_origins_split_on_commas() -> None:
    s = Settings(_env_file=None, CORS_ORIGINS="http://a.test, http://b.test")
    assert s.cors_origin_list == ["http://a.test", "http://b.test"]


def test_secret_values_returns_only_set_secrets() -> None:
    s = Settings(
        _env_file=None,
        SUPABASE_SERVICE_ROLE_KEY="srk-1",
        TELEGRAM_BOT_TOKEN="bot-2",
    )
    assert s.secret_values() == ["srk-1", "bot-2"]
    s3 = Settings(_env_file=None, OPENAI_API_KEY="oa-3")
    assert s3.secret_values() == ["oa-3"]


def test_vercel_max_duration_300() -> None:
    config = json.loads(Path(API_DIR, "vercel.json").read_text(encoding="utf-8"))
    assert all(f["maxDuration"] == 300 for f in config["functions"].values())
