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

ClockMode = Literal["frozen", "offset"]


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
    PROMPT_VERSION: str = "v1"
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
