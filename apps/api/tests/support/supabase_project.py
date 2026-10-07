"""The Supabase TEST project for `integration` contract suites (T063, T066, Section 28).

Credentials come from `apps/api/.env.test` ONLY (never `apps/api/.env`, the dev project):
`SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`. Without the file the suites skip. The project
must also carry `tests/sql/test_project_reset.sql`; `guard_test_project` skips otherwise, so a
real project named by mistake is never written to.
"""

from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings

API_DIR = Path(__file__).resolve().parents[2]
ENV_TEST = API_DIR / ".env.test"
RESET_SQL = "apps/api/tests/sql/test_project_reset.sql"


def load_test_settings() -> Settings:
    """Settings for the test project, or skip. Reads only the two Supabase keys."""
    from dotenv import dotenv_values

    if not ENV_TEST.is_file():
        pytest.skip("integration: create apps/api/.env.test for the Supabase TEST project")
    values = dotenv_values(ENV_TEST)
    url, key = values.get("SUPABASE_URL"), values.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        pytest.skip("integration: .env.test needs SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY")
    return Settings(_env_file=None, SUPABASE_URL=url, SUPABASE_SERVICE_ROLE_KEY=key)


def _rpc(client: Any, name: str) -> Any:
    from postgrest.exceptions import APIError

    try:
        return client.rpc(name, {}).execute().data
    except APIError as error:
        if error.code in ("PGRST202", "42883"):  # function not found
            pytest.skip(f"integration: apply {RESET_SQL} to the TEST project first")
        raise


def guard_test_project(client: Any) -> None:
    """Skip unless the project is marked as the test project."""
    if _rpc(client, "ledgerline_test_project") is not True:
        pytest.skip(f"integration: apply {RESET_SQL} to the TEST project first")


def reset_test_project(client: Any) -> None:
    """Empty every table (test project only), so each contract test starts fresh."""
    guard_test_project(client)
    _rpc(client, "ledgerline_test_reset")
