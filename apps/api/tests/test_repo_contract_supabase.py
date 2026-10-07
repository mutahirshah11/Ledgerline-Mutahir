"""T063/T066: the shared Repo contract suite (test_repo_contract.py) against `SupabaseRepo`.

`integration` only: needs the Supabase TEST project in `apps/api/.env.test` carrying
`tests/sql/test_project_reset.sql` (tests/support/supabase_project.py); skipped otherwise.
Every contract test that takes `repo` is re-collected here with `repo` = a `SupabaseRepo` on a
freshly emptied test project.
"""

import inspect

import pytest

import tests.test_repo_contract as contract
from app.core.config import Settings
from app.core.supabase_repo import SupabaseRepo, create_service_client
from tests.support.supabase_project import load_test_settings, reset_test_project

pytestmark = pytest.mark.integration

for _name, _test in vars(contract).items():
    if _name.startswith("test_") and "repo" in inspect.signature(_test).parameters:
        globals()[_name] = _test


@pytest.fixture(scope="module")
def supabase_settings() -> Settings:
    return load_test_settings()


@pytest.fixture
def repo(supabase_settings: Settings) -> SupabaseRepo:
    """Overrides conftest's `repo` for the tests collected in this module."""
    client = create_service_client(supabase_settings)
    reset_test_project(client)
    return SupabaseRepo(supabase_settings, client=client)
