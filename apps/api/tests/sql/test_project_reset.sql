-- =============================================================================================
-- TEST PROJECT ONLY. Never apply this to the dev or production Supabase project.
--
-- Apply once to the separate Supabase TEST project (the one in apps/api/.env.test), after
-- supabase/migrations/0001_init.sql, in its SQL editor. The integration contract suites
-- (tests/test_repo_contract_supabase.py, tests/test_blobstore_supabase.py) refuse to run
-- unless `ledgerline_test_project()` exists, so pointing .env.test at a real project skips
-- instead of writing into it. `ledgerline_test_reset()` empties every table before each Repo
-- contract test (the contract assumes a fresh store, like MemoryRepo); it lifts the audit_log
-- append-only triggers only inside its own transaction.
-- =============================================================================================

create or replace function ledgerline_test_project()
returns boolean
language sql
as $$ select true $$;

create or replace function ledgerline_test_reset()
returns void
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
    alter table audit_log disable trigger audit_log_no_update_delete;
    alter table audit_log disable trigger audit_log_no_truncate;
    truncate table
        audit_log, run_events, notifications, proofs, review_items, control_flags,
        normalisation_log, source_rows, groups, extractions, page_manifest, source_files,
        runs, allowed_users
        restart identity;
    alter table audit_log enable trigger audit_log_no_truncate;
    alter table audit_log enable trigger audit_log_no_update_delete;
end;
$$;

revoke all on function ledgerline_test_project() from public, anon, authenticated;
revoke all on function ledgerline_test_reset() from public, anon, authenticated;
grant execute on function ledgerline_test_project() to service_role;
grant execute on function ledgerline_test_reset() to service_role;
