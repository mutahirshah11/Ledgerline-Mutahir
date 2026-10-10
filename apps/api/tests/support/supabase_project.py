"""The Supabase TEST project for `integration` contract suites (T063, T066, Section 28).

Credentials come from `apps/api/.env.test` ONLY: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`
and `SUPABASE_TEST_PROJECT_REF` (the `<ref>` of `https://<ref>.supabase.co`). Without the file
the suites skip. They FAIL (never write) when `.env.test` names the same project as
`apps/api/.env` (the dev project), when the ref does not match the URL host, or when the
project's `ledgerline_test_project()` (tests/sql/test_project_reset.sql) returns another ref.
URLs are compared in Python only and never printed.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

from app.core.config import Settings

API_DIR = Path(__file__).resolve().parents[2]
ENV_TEST = API_DIR / ".env.test"
ENV_DEV = API_DIR / ".env"
RESET_SQL = "apps/api/tests/sql/test_project_reset.sql"


@dataclass(frozen=True)
class SupabaseTestProject:
    settings: Settings
    ref: str
    anon_key: str | None = None


def _same_url(a: str, b: str) -> bool:
    return a.strip().rstrip("/").lower() == b.strip().rstrip("/").lower()


def load_test_project(env_test: Path = ENV_TEST, env_dev: Path = ENV_DEV) -> SupabaseTestProject:
    """The test project from `env_test`, or skip (no file) / fail (unsafe configuration)."""
    from dotenv import dotenv_values

    if not env_test.is_file():
        pytest.skip("integration: create apps/api/.env.test for the Supabase TEST project")
    values = dotenv_values(env_test)
    url, key = values.get("SUPABASE_URL"), values.get("SUPABASE_SERVICE_ROLE_KEY")
    ref = values.get("SUPABASE_TEST_PROJECT_REF")
    if not url or not key:
        pytest.skip("integration: .env.test needs SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY")
    dev_url = dotenv_values(env_dev).get("SUPABASE_URL") if env_dev.is_file() else None
    if dev_url and _same_url(dev_url, url):
        pytest.fail("integration: .env.test names the dev project of apps/api/.env; refusing")
    if not ref:
        pytest.fail("integration: .env.test needs SUPABASE_TEST_PROJECT_REF")
    host = (urlparse(url).hostname or "").lower()
    if host.split(".")[0] != ref.lower():
        pytest.fail("integration: SUPABASE_TEST_PROJECT_REF does not match the SUPABASE_URL host")
    settings = Settings(_env_file=None, SUPABASE_URL=url, SUPABASE_SERVICE_ROLE_KEY=key)
    return SupabaseTestProject(settings, ref, anon_key=values.get("SUPABASE_ANON_KEY"))


def create_anon_client(project: SupabaseTestProject) -> Any:
    """A client using the test project's anon key, for RLS-denial checks (007-ship FR-006)."""
    if not project.anon_key:
        pytest.skip("integration: .env.test needs SUPABASE_ANON_KEY for anon-RLS checks")
    from supabase import create_client
    from supabase.lib.client_options import SyncClientOptions

    options = SyncClientOptions(auto_refresh_token=False, persist_session=False)
    assert project.settings.SUPABASE_URL is not None
    return create_client(project.settings.SUPABASE_URL, project.anon_key, options=options)


def _rpc(client: Any, name: str, params: dict[str, Any] | None = None) -> Any:
    from postgrest.exceptions import APIError

    try:
        return client.rpc(name, params or {}).execute().data
    except APIError as error:
        if error.code in ("PGRST202", "42883"):  # function not found
            pytest.skip(f"integration: apply {RESET_SQL} to the TEST project first")
        raise


def guard_test_project(client: Any, ref: str) -> None:
    """Fail unless the project marks itself as the test project with this `ref`."""
    if _rpc(client, "ledgerline_test_project") != ref:
        pytest.fail(f"integration: ledgerline_test_project() does not return the ref ({RESET_SQL})")


def reset_test_project(client: Any, ref: str) -> None:
    """Empty every table (test project only), so each contract test starts fresh."""
    guard_test_project(client, ref)
    _rpc(client, "ledgerline_test_reset", {"p_ref": ref})  # the database re-checks the ref
