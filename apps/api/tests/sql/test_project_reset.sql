-- =============================================================================================
-- TEST PROJECT ONLY. Never apply this to the dev or production Supabase project.
--
-- Apply once to the separate Supabase TEST project (the one in apps/api/.env.test), after
-- supabase/migrations/0001_init.sql, in its SQL editor. The integration contract suites
-- (tests/test_repo_contract_supabase.py, tests/test_blobstore_supabase.py) refuse to run
-- unless `ledgerline_test_project()` returns the SUPABASE_TEST_PROJECT_REF of .env.test, so
-- pointing .env.test at a real project never writes into it.
--
-- BEFORE APPLYING: replace REPLACE_WITH_TEST_PROJECT_REF below with the test project's ref
-- (the <ref> of https://<ref>.supabase.co).
--
-- `ledgerline_test_reset(p_ref)` empties every table before each Repo contract test (the
-- contract assumes a fresh store, like MemoryRepo). It refuses unless p_ref equals
-- ledgerline_test_project(), and lifts the audit_log append-only triggers only inside its
-- own transaction.
-- =============================================================================================

drop function if exists ledgerline_test_project();  -- earlier version returned boolean

create or replace function ledgerline_test_project()
returns text
language sql
as $$ select 'REPLACE_WITH_TEST_PROJECT_REF'::text $$;

drop function if exists ledgerline_test_reset();  -- earlier version took no ref

create or replace function ledgerline_test_reset(p_ref text)
returns void
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
    if p_ref is distinct from ledgerline_test_project() then
        raise exception 'ledgerline_test_reset: ref % is not this test project', p_ref
            using errcode = 'insufficient_privilege';
    end if;
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
revoke all on function ledgerline_test_reset(text) from public, anon, authenticated;
grant execute on function ledgerline_test_project() to service_role;
grant execute on function ledgerline_test_reset(text) to service_role;
