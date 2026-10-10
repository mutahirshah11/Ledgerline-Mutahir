"""007-ship US2/US3 (contracts/readme-outline.md, contracts/nfr-report.md): `README.md` is the
only doc a reviewer reads before the 15-minute fresh-clone clock starts, and NFR-07 (no
pandas/numpy in the deployed function) is checked the same way, offline, every commit. T025 to
T029, T041.
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


def test_P6_no_pandas_numpy() -> None:
    """NFR-07: the deployed Python function stays small; pandas/numpy alone would blow past the
    250 MB uncompressed limit."""
    lock_text = (API_DIR / "uv.lock").read_text(encoding="utf-8")
    for package in ("pandas", "numpy"):
        assert f'name = "{package}"' not in lock_text, f"{package} must never be a dependency"


NFR_REPORT = REPO_ROOT / "NFR_REPORT.md"
EXPECTED_NFR_IDS = tuple(f"NFR-{n:02d}" for n in range(1, 12))
TIMED_NFR_IDS = frozenset({"NFR-01", "NFR-02", "NFR-05", "NFR-11"})
NFR_RESULTS = frozenset({"MET", "NOT MET", "NOT MEASURED"})


def _nfr_table_1_rows() -> list[list[str]]:
    if not NFR_REPORT.is_file():
        pytest.fail("NFR_REPORT.md does not exist at the repo root")
    text = NFR_REPORT.read_text(encoding="utf-8")
    start = text.index("## Table 1")
    end = text.index("## Table 2")
    lines = [line for line in text[start:end].splitlines() if line.startswith("| NFR-")]
    return [[cell.strip() for cell in line.strip("|").split("|")] for line in lines]


def test_P6_nfr_report_complete() -> None:
    rows = _nfr_table_1_rows()
    assert [row[0] for row in rows] == list(EXPECTED_NFR_IDS)
    for nfr_id, _requirement, _target, _method, values, result, evidence, _env, _date in rows:
        assert result in NFR_RESULTS, f"{nfr_id}: Result must be one of {sorted(NFR_RESULTS)}"
        if result == "NOT MET":
            assert evidence, f"{nfr_id}: a NOT MET row needs a one-line reason in Evidence"
        if result == "MET":
            assert evidence, f"{nfr_id}: a MET row needs non-empty Evidence"
            assert values, f"{nfr_id}: a MET row needs non-empty Values"
            if nfr_id in TIMED_NFR_IDS:
                assert len(values.split(",")) == 3, f"{nfr_id}: a timed MET row needs 3 values"


def test_P6_nfr_report_table_2_has_six_rows() -> None:
    if not NFR_REPORT.is_file():
        pytest.fail("NFR_REPORT.md does not exist at the repo root")
    text = NFR_REPORT.read_text(encoding="utf-8")
    start = text.index("## Table 2")
    end = text.index("## Table 3")
    rows = [
        line for line in text[start:end].splitlines() if line.startswith("|") and "---" not in line
    ]
    assert len(rows) == 1 + 6  # header + 6 metric rows
