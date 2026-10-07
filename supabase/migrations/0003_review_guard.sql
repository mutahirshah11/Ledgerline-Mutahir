-- =============================================================================================
-- Ledgerline 0003_review_guard.sql: no review decision on a FINALISED run (005 final review I-6;
-- F-08.R8, F-09.R5). Additive only: one trigger function, one trigger. Apply after 0002.
--
--   review_items: no update while the run is FINALISED. EP-15 checks the run status first, but
--   a finalise can commit between that check and the decision's compare-and-set; this trigger
--   closes that window (Repo.resolve_review_item, and the upsert_review_items refresh).
--
-- Error (message prefix = API error code), mapped by SupabaseRepo:
--   RUN_STATE_CONFLICT  (55000) the run is FINALISED                → StateConflict(run)
-- Postgres 17 (Supabase).
-- =============================================================================================

create or replace function review_items_refuse_finalised_run()
returns trigger
language plpgsql
set search_path = public, pg_temp
as $$
declare
    v_status run_status;
begin
    -- share-lock the run row first, whatever its status (see 0002 proofs_refuse_finalised_run):
    -- a concurrent finalise then waits for this transaction, and the status read below is the
    -- one that holds until commit
    select r.status into v_status from runs r where r.id = new.run_id for share;
    if v_status = 'FINALISED' then
        raise exception 'RUN_STATE_CONFLICT: run % is FINALISED', new.run_id
            using errcode = 'object_not_in_prerequisite_state';
    end if;
    return new;
end;
$$;

create trigger review_items_refuse_finalised_run
    before update on review_items
    for each row execute function review_items_refuse_finalised_run();

revoke all on function review_items_refuse_finalised_run() from public, anon, authenticated;
