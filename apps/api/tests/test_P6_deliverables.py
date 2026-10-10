"""007-ship US2 (contracts/readme-outline.md): `README.md` is the only doc a reviewer reads
before the 15-minute fresh-clone clock starts, so these checks run every commit, offline. T025
to T029.
"""

import re
from pathlib import Path

import pytest

from app.core.config import Settings

API_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = API_DIR.parents[1]
README = REPO_ROOT / "README.md"
WEB_DIR = REPO_ROOT / "apps" / "web"

EXPECTED_HEADINGS = (
    "What it does",
    "How to run",
    "Assumptions",
    "How it fails and how someone would know",
    "How the data was handled",
    "What I'd do with another week",
    "AI tools used and for what",
)

BADGE_RE = re.compile(
    r"^\[!\[CI\]\(https://github\.com/mutahirshah11/Ledgerline-Mutahir/actions/workflows/"
    r"ci\.yml/badge\.svg\?branch=main\)\]\(https://github\.com/mutahirshah11/"
    r"Ledgerline-Mutahir/actions/workflows/ci\.yml\)$",
    re.MULTILINE,
)

ENV_VAR_RE = re.compile(r"`([A-Z][A-Z0-9_]*)`")
PROCESS_ENV_RE = re.compile(r"process\.env\.([A-Z][A-Z0-9_]*[A-Z0-9])\b")

# Found under apps/web/ (excluding node_modules) but never set by a reviewer following the
# README's local steps: the Playwright/e2e harness sets these for itself. NODE_ENV is Node's own
# variable, not one this codebase reads.
WEB_HARNESS_ONLY_VARS = frozenset(
    {
        "NODE_ENV",
        "BASE_URL",
        "CI",
        "ENABLE_TEST_PAGES",
        "NEXT_DIST_DIR",
        "PW_PORT",
        "SMOKE_EMAIL",
        "SMOKE_PASSWORD",
    }
)

SECRET_LIKE_RE = re.compile(
    r"\bsk-[A-Za-z0-9]{10,}|\beyJ[A-Za-z0-9_-]{10,}|\b\d{6,}:[A-Za-z0-9_-]{20,}"
)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ALLOWED_EMAILS = {"you@example.com"}
ALLOWED_EMAIL_DOMAIN = "kestrel.example"


def _text() -> str:
    if not README.is_file():
        pytest.fail("README.md does not exist at the repo root")
    return README.read_text(encoding="utf-8")


def _headings(text: str) -> list[str]:
    lines = text.splitlines()
    return [line.removeprefix("## ").strip() for line in lines if line.startswith("## ")]


def test_P6_readme_headings_in_order() -> None:
    assert _headings(_text()) == list(EXPECTED_HEADINGS)


def _word_count(text: str) -> int:
    body = BADGE_RE.sub("", text)
    body = re.sub(r"[|`*#>-]", " ", body)
    return len([w for w in body.split() if w not in ("---", ":--", "--:")])


def test_P6_readme_word_limit() -> None:
    assert _word_count(_text()) <= 1100


def test_P6_readme_badge() -> None:
    text = _text()
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines[0].startswith("# ")
    assert BADGE_RE.search(text), "CI badge for ci.yml on main must sit right under the title"
    badge_line_index = next(i for i, line in enumerate(lines) if BADGE_RE.match(line))
    assert badge_line_index == 1, "CI badge must be the line directly under the title"


def _web_runtime_vars() -> set[str]:
    found: set[str] = set()
    for path in WEB_DIR.rglob("*.ts*"):
        if "node_modules" in path.parts:
            continue
        found |= set(PROCESS_ENV_RE.findall(path.read_text(encoding="utf-8", errors="ignore")))
    return found - WEB_HARNESS_ONLY_VARS


# Named in the README's "test-only" line (apps/api/.env.test), never read via Settings/env by
# the shipped app itself: the integration-test harness reads them straight from the dotenv file.
TEST_HARNESS_ONLY_VARS = frozenset({"SUPABASE_ANON_KEY", "SUPABASE_TEST_PROJECT_REF"})

# Backticked ALL-CAPS tokens in "How to run" that name something other than an env var (e.g. a
# role), so the env-table check doesn't mistake them for one.
NON_ENV_VAR_TOKENS = frozenset({"CONTROLLER"})


def test_P6_readme_env_table_matches_code() -> None:
    code_vars = set(Settings.model_fields) | {"BUILDER_EMAIL"} | _web_runtime_vars()
    allowed_extra = TEST_HARNESS_ONLY_VARS | WEB_HARNESS_ONLY_VARS | NON_ENV_VAR_TOKENS
    text = _text()
    how_to_run = text[text.index("## How to run") : text.index("## Assumptions")]
    readme_vars = set(ENV_VAR_RE.findall(how_to_run))
    missing = code_vars - readme_vars
    extra = readme_vars - code_vars - allowed_extra
    assert not missing, f"README.md's env table is missing: {sorted(missing)}"
    assert not extra, f"README.md's env table lists variables the code never reads: {sorted(extra)}"


def test_P6_readme_no_secrets() -> None:
    text = _text()
    assert not SECRET_LIKE_RE.search(text), "README.md must never contain a key/token value"
    for email in EMAIL_RE.findall(text):
        assert email in ALLOWED_EMAILS or email.endswith(f"@{ALLOWED_EMAIL_DOMAIN}"), (
            f"README.md must not name a real email address: {email}"
        )
