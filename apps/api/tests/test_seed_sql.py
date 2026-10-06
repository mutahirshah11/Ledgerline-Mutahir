"""Static checks of `supabase/seed/allowed_users.sql` (T052).

Sources: specs/001-foundation/spec.md US8 + FR-034, quickstart.md step 6 (expects
`select count(*) from allowed_users;` = 4 after seeding: the 3 demo users here plus the
builder's own email, added separately by scripts/seed_users.py).
"""

import re

from scripts.seed_users import SEED_SQL_PATH, parse_seed_sql

EXPECTED = {
    "controller@kestrel.example": "CONTROLLER",
    "reviewer@kestrel.example": "REVIEWER",
    "ops@kestrel.example": "OPS",
}


def test_seed_inserts_three_example_users() -> None:
    rows = parse_seed_sql(SEED_SQL_PATH)

    assert len(rows) == 3
    assert {row.email: row.role for row in rows} == EXPECTED
    for row in rows:
        assert row.display_name  # non-empty

    sql = SEED_SQL_PATH.read_text(encoding="utf-8")
    assert re.search(r"on\s+conflict\s*\(\s*email\s*\)\s*do\s+nothing", sql, re.IGNORECASE)


def test_seed_contains_no_real_email() -> None:
    sql = SEED_SQL_PATH.read_text(encoding="utf-8")
    emails = re.findall(r"[\w.+-]+@[\w.-]+", sql)

    assert emails, "expected at least one email literal in the seed file"
    for email in emails:
        assert email.endswith("@kestrel.example"), f"non-.example email found: {email}"
