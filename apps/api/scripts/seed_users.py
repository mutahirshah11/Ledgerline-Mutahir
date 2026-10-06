"""Seed the `allowed_users` allowlist (US8, FR-034, Makefile `seed` target).

The 3 demo users (`.example` domain, C13/R6) live in ONE place: `supabase/seed/allowed_users.sql`.
This script parses that exact file instead of duplicating the list, so the SQL and the Python
seeder can never drift apart. It then adds the builder's own email as `CONTROLLER` (blueprint R6);
that email is never committed and comes only from the `BUILDER_EMAIL` env var or a CLI argument.

Usage::

    make seed BUILDER_EMAIL=you@example.com
    # or, directly:
    uv run --project apps/api python -m scripts.seed_users you@example.com

Requires `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` (apps/api/.env, blueprint Section 26).
The service-role key is read via `get_settings()` and is never printed or logged.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from app.core.config import Settings, get_settings

SCRIPTS_DIR = Path(__file__).resolve().parent
API_DIR = SCRIPTS_DIR.parent
REPO_ROOT = API_DIR.parent.parent
SEED_SQL_PATH = REPO_ROOT / "supabase" / "seed" / "allowed_users.sql"

_ROW_RE = re.compile(r"\(\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*\)")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True, slots=True)
class SeedUser:
    """One row to upsert into `allowed_users`."""

    email: str
    display_name: str
    role: str


def parse_seed_sql(path: Path = SEED_SQL_PATH) -> list[SeedUser]:
    """Parse the `insert into allowed_users (...) values (...), (...)` tuples in `path`.

    This is deliberately simple (no SQL parser): it only needs to understand the literal
    `('email', 'display_name', 'ROLE')` tuples written in our own committed seed file.
    """
    text = path.read_text(encoding="utf-8")
    insert_match = re.search(
        r"insert\s+into\s+allowed_users\s*\([^)]*\)\s*values\s*(.*?);",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if insert_match is None:
        raise ValueError(f"no 'insert into allowed_users (...) values (...)' statement in {path}")
    values_block = insert_match.group(1)
    rows = [
        SeedUser(email=m[0], display_name=m[1], role=m[2]) for m in _ROW_RE.findall(values_block)
    ]
    if not rows:
        raise ValueError(f"no value tuples parsed from {path}")
    return rows


def validate_email(raw: str) -> str:
    """Lower-case and validate `raw` (the `allowed_users` CHECK requires lower-case email)."""
    candidate = raw.strip().lower()
    if not candidate or not _EMAIL_RE.match(candidate):
        raise ValueError(
            f"BUILDER_EMAIL is not a valid email address: {raw!r} "
            "(pass `make seed BUILDER_EMAIL=you@example.com` or set the BUILDER_EMAIL env var)"
        )
    return candidate


def build_rows(demo_users: list[SeedUser], builder_email: str) -> list[dict[str, str]]:
    """The demo users plus `builder_email` as CONTROLLER (skipped if it matches a demo user)."""
    email = validate_email(builder_email)
    rows = [{"email": u.email, "display_name": u.display_name, "role": u.role} for u in demo_users]
    if email not in {u.email for u in demo_users}:
        rows.append({"email": email, "display_name": "Builder (CONTROLLER)", "role": "CONTROLLER"})
    return rows


class _Table(Protocol):
    def upsert(self, rows: list[dict[str, str]], on_conflict: str) -> _Table: ...
    def execute(self) -> object: ...


class _SupabaseClient(Protocol):
    def table(self, name: str) -> _Table: ...


def get_client(settings: Settings) -> _SupabaseClient:
    """Build a Supabase client from `settings`, failing loudly if config is missing.

    Never prints the service-role key.
    """
    if not settings.SUPABASE_URL:
        raise RuntimeError(
            "SUPABASE_URL is not set. Fill it in apps/api/.env (blueprint Section 26)."
        )
    if settings.SUPABASE_SERVICE_ROLE_KEY is None:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY is not set. Fill it in apps/api/.env "
            "(blueprint Section 26); it is never logged or printed."
        )

    from supabase import create_client

    return cast(
        "_SupabaseClient",
        create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY.get_secret_value()),
    )


def run_seed(client: _SupabaseClient, demo_users: list[SeedUser], builder_email: str) -> None:
    """Upsert the demo users plus `builder_email` through `client`."""
    rows = build_rows(demo_users, builder_email)
    client.table("allowed_users").upsert(rows, on_conflict="email").execute()


def _resolve_builder_email(argv: list[str]) -> str:
    import os

    if argv:
        return argv[0]
    env_value = os.environ.get("BUILDER_EMAIL", "")
    if not env_value:
        raise RuntimeError(
            "BUILDER_EMAIL is required: `make seed BUILDER_EMAIL=you@example.com` "
            "(it is never committed to a file)."
        )
    return env_value


def main(argv: list[str] | None = None) -> None:
    builder_email = _resolve_builder_email(sys.argv[1:] if argv is None else argv)
    demo_users = parse_seed_sql(SEED_SQL_PATH)
    settings = get_settings()
    client = get_client(settings)
    run_seed(client, demo_users, builder_email)
    print(f"Seeded {len(demo_users) + 1} allowed_users rows (service-role key not shown).")


if __name__ == "__main__":
    main()
