"""Unit tests for scripts/seed_users.py pure logic (T053). No network: a FakeClient stands in
for the supabase client, and build_rows()/validate_email() are tested directly.
"""

from pathlib import Path

import pytest

from app.core.config import Settings
from scripts.seed_users import (
    SeedUser,
    build_rows,
    get_client,
    parse_seed_sql,
    run_seed,
    validate_email,
)

_REPO_ROOT = Path(__file__).resolve().parents[1].parent.parent
SEED_SQL_PATH = _REPO_ROOT / "supabase" / "seed" / "allowed_users.sql"

DEMO_USERS = [
    SeedUser("controller@kestrel.example", "Controller (Demo)", "CONTROLLER"),
    SeedUser("reviewer@kestrel.example", "Reviewer (Demo)", "REVIEWER"),
    SeedUser("ops@kestrel.example", "Ops (Demo)", "OPS"),
]


class FakeTable:
    def __init__(self, recorder: list[tuple[str, list[dict[str, str]], str]]) -> None:
        self._recorder = recorder

    def upsert(self, rows: list[dict[str, str]], on_conflict: str) -> "FakeTable":
        self._recorder.append(("upsert", rows, on_conflict))
        return self

    def execute(self) -> None:
        self._recorder.append(("execute", [], ""))


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict[str, str]], str]] = []

    def table(self, name: str) -> FakeTable:
        assert name == "allowed_users"
        return FakeTable(self.calls)


def test_build_rows_adds_builder_email_as_controller() -> None:
    rows = build_rows(DEMO_USERS, builder_email="Builder@Example.com")

    assert rows[-1] == {
        "email": "builder@example.com",
        "display_name": "Builder (CONTROLLER)",
        "role": "CONTROLLER",
    }
    assert len(rows) == 4
    assert rows[0] == {
        "email": "controller@kestrel.example",
        "display_name": "Controller (Demo)",
        "role": "CONTROLLER",
    }


def test_build_rows_deduplicates_builder_email_matching_a_demo_user() -> None:
    rows = build_rows(DEMO_USERS, builder_email="reviewer@kestrel.example")

    assert len(rows) == 3
    assert {row["email"] for row in rows} == {u.email for u in DEMO_USERS}


def test_validate_email_lowercases_and_accepts_valid_address() -> None:
    assert validate_email("Someone@Example.com") == "someone@example.com"


@pytest.mark.parametrize(
    "bad", ["", "not-an-email", "a@b", "@example.com", "a@", "a b@example.com"]
)
def test_validate_email_rejects_invalid_address(bad: str) -> None:
    with pytest.raises(ValueError, match="BUILDER_EMAIL"):
        validate_email(bad)


def test_get_client_raises_loudly_when_url_missing() -> None:
    settings = Settings(_env_file=None, SUPABASE_URL="", SUPABASE_SERVICE_ROLE_KEY=None)

    with pytest.raises(RuntimeError, match="SUPABASE_URL"):
        get_client(settings)


def test_get_client_raises_loudly_when_service_role_key_missing() -> None:
    settings = Settings(_env_file=None, SUPABASE_URL="https://example.supabase.co")

    with pytest.raises(RuntimeError, match="SUPABASE_SERVICE_ROLE_KEY"):
        get_client(settings)


def test_run_seed_upserts_rows_through_injected_client() -> None:
    fake = FakeClient()

    run_seed(fake, DEMO_USERS, builder_email="builder@example.com")

    assert len(fake.calls) == 2
    kind, rows, on_conflict = fake.calls[0]
    assert kind == "upsert"
    assert on_conflict == "email"
    assert len(rows) == 4
    assert fake.calls[1][0] == "execute"


def test_parse_seed_sql_matches_committed_file() -> None:
    rows = parse_seed_sql(SEED_SQL_PATH)

    assert rows == DEMO_USERS
