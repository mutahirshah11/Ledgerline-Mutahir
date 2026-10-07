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


FINALISE_FN = "runs_refuse_unreconciled_finalise"


def test_F10_R2_runs_refuse_unreconciled_finalise_trigger() -> None:
    """IMP-1: no `FINALISED` while a decided item is missing from the newest `RECONCILED`
    audit's `decided_item_keys` (`lifecycle.unreconciled_decisions`)."""
    text = _sql()
    m = re.search(
        rf"create or replace function {FINALISE_FN} ?\(\) returns trigger (.*?)\$\$;", text
    )
    assert m, FINALISE_FN
    fn = m.group(1)
    assert "set search_path = public, pg_temp" in fn
    assert "a.after -> 'decided_item_keys'" in fn
    assert "a.run_id = new.id and a.action = 'reconciled'" in fn
    assert "order by a.id desc limit 1" in fn  # newest by id, not by `at`
    assert "ri.status <> 'open'" in fn and "not (v_applied ? ri.item_key)" in fn
    assert "for update" not in fn and "for share" not in fn  # reads only: no lock cycle
    assert "'run_state_conflict: a review decision is still being applied" in fn
    assert "errcode = 'object_not_in_prerequisite_state'" in fn
    assert re.search(
        rf"create trigger {FINALISE_FN} before update on runs for each row "
        rf"when \(new\.status = 'finalised' and old\.status is distinct from 'finalised'\) "
        rf"execute function {FINALISE_FN}\(\)",
        text,
    )
    assert f"revoke all on function {FINALISE_FN}() from public, anon, authenticated;" in text


def test_F10_R2_engine_writes_the_key_the_trigger_reads() -> None:
    from app.domain.lifecycle import DECIDED_KEYS

    assert f"'{DECIDED_KEYS}'" in _sql()


def test_F10_R2_smoke_covers_finalise_guard_and_refresh() -> None:
    smoke = (REPO_ROOT / "supabase" / "tests" / "smoke.sql").read_text(encoding="utf-8")
    assert "FINALISED with an unreconciled decision" in smoke
    assert "upsert refresh on a FINALISED run" in smoke
