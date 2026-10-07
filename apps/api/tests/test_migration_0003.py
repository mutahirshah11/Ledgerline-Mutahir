"""Static checks of `supabase/migrations/0003_review_guard.sql` (005 final review I-6): no review
decision lands on a FINALISED run. Offline, like test_migration_0002.py; the behaviour runs in
supabase/tests/smoke.sql (CI db job)."""

import re

from tests.sql_helpers import REPO_ROOT, strip_comments

PATH = REPO_ROOT / "supabase" / "migrations" / "0003_review_guard.sql"
FN = "review_items_refuse_finalised_run"


def _sql() -> str:
    return re.sub(r"\s+", " ", strip_comments(PATH.read_text(encoding="utf-8"))).lower()


def test_additive_only() -> None:
    text = _sql()
    for banned in (r"\bdrop\b", r"\btruncate\b", r"alter table \w+ (drop|alter|rename)"):
        assert not re.search(banned, text), banned


def test_F08_R8_review_items_refuse_finalised_run_trigger() -> None:
    text = _sql()
    m = re.search(rf"create or replace function {FN} ?\(\) returns trigger (.*?)\$\$;", text)
    assert m, FN
    fn = m.group(1)
    assert "set search_path = public, pg_temp" in fn
    # lock the run row unconditionally, then check (0002 proofs_refuse_finalised_run)
    assert re.search(
        r"select r\.status into v_status from runs r where r\.id = new\.run_id for share;", fn
    )
    assert not re.search(r"where[^;]*status[^;]*for share", fn)
    assert "v_status = 'finalised'" in fn
    assert "'run_state_conflict: " in fn
    assert "errcode = 'object_not_in_prerequisite_state'" in fn  # 55000 → StateConflict(run)
    assert re.search(
        rf"create trigger {FN} before update on review_items for each row "
        rf"execute function {FN}\(\)",
        text,
    )
    assert f"revoke all on function {FN}() from public, anon, authenticated;" in text


def test_smoke_and_ci_apply_0003() -> None:
    runner = (REPO_ROOT / "supabase" / "tests" / "run_smoke.sh").read_text(encoding="utf-8")
    assert "0003_review_guard.sql" in runner
    smoke = (REPO_ROOT / "supabase" / "tests" / "smoke.sql").read_text(encoding="utf-8")
    assert "decision on a FINALISED run" in smoke
