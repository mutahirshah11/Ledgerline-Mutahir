"""Process settings from environment variables (blueprint Section 26, research R9, FR-025)."""

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import AwareDatetime, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

API_DIR = Path(__file__).resolve().parents[2]

BACKDATE_DAYS = 1
"""F-06: an ERP posting more than this many days after the register date is backdated."""

EXTRACT_TIMEOUT_S = 60
"""F-03.R5: seconds before one page-read call times out."""
EXTRACT_ATTEMPTS = 3
"""F-03.R5: attempts per page read, including the first."""
EXTRACT_WAITS_S = (2, 8)
"""F-03.R5: seconds to wait before the second and third attempts."""

ClockMode = Literal["frozen", "offset"]
RepoBackend = Literal["memory", "supabase"]


class Settings(BaseSettings):
    """Every Section 26 variable the API reads. Secrets may be blank in Phase 0."""

    model_config = SettingsConfigDict(
        env_file=API_DIR / ".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        case_sensitive=True,
        extra="ignore",
    )

    SUPABASE_URL: str = ""
    SUPABASE_SERVICE_ROLE_KEY: SecretStr | None = None

    OPENAI_API_KEY: SecretStr | None = None
    EXTRACT_MODEL: str = "gpt-5.6-luna"
    PROMPT_VERSION: str = "v3"
    EXTRACT_PRICE_INPUT_PER_MTOK: Decimal | None = None
    EXTRACT_PRICE_OUTPUT_PER_MTOK: Decimal | None = None

    TELEGRAM_BOT_TOKEN: SecretStr | None = None
    TELEGRAM_CONTROLLER_CHAT_ID: str | None = None
    TELEGRAM_OPS_CHAT_ID: str | None = None

    LEDGERLINE_NOW: AwareDatetime | None = None
    LEDGERLINE_CLOCK_MODE: ClockMode | None = None
    LEDGERLINE_NOW_ANCHOR: AwareDatetime | None = None

    ALLOW_FAULTS: bool = False

    APP_BASE_URL: str = "http://localhost:3000"
    API_BASE_URL: str = "http://localhost:8000"
    CORS_ORIGINS: str = "http://localhost:3000"

    OPENAI_TIMEOUT_S: int = EXTRACT_TIMEOUT_S
    EXTRACT_ATTEMPTS: int = EXTRACT_ATTEMPTS
    EXTRACT_BACKOFF_S: tuple[int, ...] = EXTRACT_WAITS_S

    TELEGRAM_ATTEMPTS: int = 3
    TELEGRAM_BACKOFF_S: tuple[int, ...] = (1, 4)
    TELEGRAM_TIMEOUT_S: int = 10

    UPLOAD_WINDOW_S: int = 600
    """Section 10 / C19: seconds a signed upload stays valid for EP-07."""
    DOWNLOAD_URL_S: int = 300
    JWKS_CACHE_S: int = 600

    REPO_BACKEND: RepoBackend = "supabase"
    """Tests select `memory`; anything else uses Supabase (004 data-model §5)."""

    @property
    def cors_origin_list(self) -> list[str]:
        """`CORS_ORIGINS` split on commas, blanks dropped."""
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    def secret_values(self) -> list[str]:
        """The set secret values, for log redaction (F-11.R3). Never log the result."""
        secrets = (self.SUPABASE_SERVICE_ROLE_KEY, self.OPENAI_API_KEY, self.TELEGRAM_BOT_TOKEN)
        return [s.get_secret_value() for s in secrets if s is not None and s.get_secret_value()]

    @property
    def clock_mode(self) -> ClockMode:
        """`frozen` unless `offset` is chosen explicitly (contracts/clock.md)."""
        return self.LEDGERLINE_CLOCK_MODE or "frozen"

    @model_validator(mode="after")
    def _offset_needs_anchor(self) -> Self:
        if (
            self.LEDGERLINE_NOW is not None
            and self.clock_mode == "offset"
            and self.LEDGERLINE_NOW_ANCHOR is None
        ):
            raise ValueError("LEDGERLINE_NOW_ANCHOR is required when LEDGERLINE_CLOCK_MODE=offset")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The one settings object for this process, loaded and validated on first use."""
    return Settings()
