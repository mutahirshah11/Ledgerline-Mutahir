"""Static checks of `supabase/migrations/0002_api_guards.sql` (P3 review, research R12): the
review-item upsert RPC and the run-state triggers SupabaseRepo relies on. Offline, like the
0001 checks in test_migration_tables.py."""

import re

from tests.sql_helpers import REPO_ROOT, strip_comments

PATH = REPO_ROOT / "supabase" / "migrations" / "0002_api_guards.sql"


def _sql() -> str:
    return re.sub(r"\s+", " ", strip_comments(PATH.read_text(encoding="utf-8"))).lower()


def _function(name: str) -> str:
    text = _sql()
    m = re.search(rf"create or replace function {name} ?\(", text)
    assert m, name
    tag = re.compile(r"as (\$\w*\$)").search(text, m.end())
    assert tag
    return text[m.start() : text.find(tag.group(1), tag.end()) + len(tag.group(1))]


def test_additive_only() -> None:
    text = _sql()
    for banned in (r"\bdrop\b", r"\btruncate\b", r"alter table \w+ (drop|alter|rename)"):
        assert not re.search(banned, text), banned


def test_upsert_review_items_rpc() -> None:
    fn = _function("upsert_review_items")
    assert re.search(r"upsert_review_items ?\(p_run_id uuid, ?p_items jsonb\)", fn)
    assert "returns setof review_items" in fn
    assert "security invoker" in fn and "security definer" not in fn
    assert "set search_path = public, pg_temp" in fn
    conflict = re.search(r"on conflict ?\(run_id, ?item_key\) do update set (.*?);", fn)
    assert conflict, "upsert on the natural key"
    refreshed = set(re.findall(r"(\w+) = excluded\.\w+", conflict.group(1)))
    assert refreshed == {
        "reason",
        "read_a",
        "read_b",
        "context",
        "grn_no",
        "source_row_id",
        "field",
    }
    assert "'open'" in fn  # new items start OPEN
    assert "errcode = 'no_data_found'" in fn  # NOT_FOUND for a missing run
    assert "constraint = 'review_items_run_item_key_uq'" in fn  # batch repeats: 23505


def test_run_state_triggers() -> None:
    text = _sql()
    for fn_name, table, event, state in (
        ("proofs_refuse_finalised_run", "proofs", "insert or update", "'finalised'"),
        ("source_files_delete_only_created", "source_files", "delete", "'created'"),
        ("source_files_insert_only_created", "source_files", "insert", "'created'"),
    ):
        fn = _function(fn_name)
        assert "returns trigger" in fn and state in fn
        # lock the run row unconditionally, then check: a status filter in the locking
        # query would skip the lock exactly when the write is allowed
        row = "old" if event == "delete" else "new"
        assert re.search(
            rf"select r\.status into v_status from runs r where r\.id = {row}\.run_id for share;",
            fn,
        ), fn_name
        assert not re.search(r"where[^;]*status[^;]*for share", fn), fn_name
        assert "errcode = 'object_not_in_prerequisite_state'" in fn  # 55000 → StateConflict
        assert re.search(
            rf"create trigger \w+ before {event} on {table} for each row "
            rf"execute function {fn_name}\(\)",
            text,
        ), table


def test_functions_executable_by_service_role_only() -> None:
    text = _sql()
    assert re.search(
        r"revoke all on function upsert_review_items\(uuid, ?jsonb\) "
        r"from public, anon, authenticated;",
        text,
    )
    assert re.search(
        r"grant execute on function upsert_review_items\(uuid, ?jsonb\) to service_role;", text
    )
    for trigger_fn in (
        "proofs_refuse_finalised_run",
        "source_files_delete_only_created",
        "source_files_insert_only_created",
    ):
        assert f"revoke all on function {trigger_fn}() from public, anon, authenticated;" in text
