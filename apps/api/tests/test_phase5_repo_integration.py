"""006-web carry-overs: the batched Repo reads (test_phase5_repo.py) against `SupabaseRepo`.

`integration` only: needs the Supabase TEST project in `apps/api/.env.test` carrying
`tests/sql/test_project_reset.sql` (tests/support/supabase_project.py); skipped otherwise.
Every test of test_phase5_repo.py that takes `repo` is re-collected here with `repo` = a
`SupabaseRepo` on a freshly emptied test project (as test_repo_contract_supabase.py).
"""

import inspect

import pytest

import tests.test_phase5_repo as batched
from app.core.supabase_repo import SupabaseRepo, create_service_client
from tests.support.supabase_project import (
    SupabaseTestProject,
    load_test_project,
    reset_test_project,
)

pytestmark = pytest.mark.integration

for _name, _test in vars(batched).items():
    if _name.startswith("test_") and "repo" in inspect.signature(_test).parameters:
        globals()[_name] = _test


@pytest.fixture(scope="module")
def supabase_project() -> SupabaseTestProject:
    return load_test_project()


@pytest.fixture
def repo(supabase_project: SupabaseTestProject) -> SupabaseRepo:
    """Overrides conftest's `repo` for the tests collected in this module."""
    client = create_service_client(supabase_project.settings)
    reset_test_project(client, supabase_project.ref)
    return SupabaseRepo(supabase_project.settings, client=client)
