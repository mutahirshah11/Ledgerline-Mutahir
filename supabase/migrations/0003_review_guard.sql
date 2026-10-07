-- =============================================================================================
-- Ledgerline 0003_review_guard.sql: review decisions vs finalise (005 final review I-6, IMP-1;
-- F-08.R8, F-09.R5, F-10.R2). Additive only: two trigger functions, two triggers. Apply after
-- 0002.
--
--   1. review_items: no update while the run is FINALISED. EP-15 checks the run status first,
--      but a finalise can commit between that check and the decision's compare-and-set; this
--      trigger closes that window (Repo.resolve_review_item, and the upsert_review_items
--      refresh).
--   2. runs: no move to FINALISED while a decided (not OPEN) review item has not been applied
--      by a reconcile yet. EP-15 commits the decision, then re-reconciles in later statements;
--      a finalise in between would freeze a report that lists the decision while the rows,
--      groups and proof ignore it. "Applied" = the item_key is in `decided_item_keys` of the
--      run's newest RECONCILED audit_log entry (highest id: the append order, `at` can tie);
--      the engine writes that list (the decided items it read as inputs) as its last write,
--      after the rows, groups, review items, proof and status (app.domain.lifecycle
--      .unreconciled_decisions is the same rule; MemoryRepo.transition_run mirrors it). An
--      entry without the list, or none, applied no decision. A decision is never undone, so
--      keys suffice.
--
-- Lock order (no deadlock): a decision (or upsert refresh) locks its review_items row, then
-- the trigger share-locks the run row (upsert_review_items share-locks the run first; share
-- locks never wait for each other). A finalise locks the run row (UPDATE runs) and then only
-- reads review_items and audit_log (plain SELECTs, no row locks), so it never waits for a
-- review_items row: no cycle. They serialise on the run row: a decision that share-locked it
-- first makes the finalise UPDATE wait until it commits, and the trigger body below (plpgsql,
-- volatile: a new snapshot per statement under READ COMMITTED) then sees the decision and
-- refuses; a finalise that locked it first makes the decision wait, after which trigger 1
-- sees FINALISED and refuses. Either way no decision is left out of a frozen report.
--
-- Error (message prefix = API error code), mapped by SupabaseRepo:
--   RUN_STATE_CONFLICT  (55000) the run is FINALISED, or a decision is not reconciled yet
--                               → StateConflict(run)
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


create or replace function runs_refuse_unreconciled_finalise()
returns trigger
language plpgsql
set search_path = public, pg_temp
as $$
declare
    v_applied jsonb;
    v_key     text;
begin
    -- the newest reconcile marker of this run (highest id); a missing list applied nothing
    select coalesce(a.after -> 'decided_item_keys', '[]'::jsonb) into v_applied
    from audit_log a
    where a.run_id = new.id and a.action = 'RECONCILED'
    order by a.id desc
    limit 1;
    if jsonb_typeof(v_applied) is distinct from 'array' then
        v_applied := '[]'::jsonb;
    end if;
    select ri.item_key into v_key
    from review_items ri
    where ri.run_id = new.id
      and ri.status <> 'OPEN'
      and not (v_applied ? ri.item_key)
    limit 1;
    if v_key is not null then
        raise exception 'RUN_STATE_CONFLICT: a review decision is still being applied (run %, %)',
            new.id, v_key
            using errcode = 'object_not_in_prerequisite_state';
    end if;
    return new;
end;
$$;

create trigger runs_refuse_unreconciled_finalise
    before update on runs
    for each row
    when (new.status = 'FINALISED' and old.status is distinct from 'FINALISED')
    execute function runs_refuse_unreconciled_finalise();

revoke all on function runs_refuse_unreconciled_finalise() from public, anon, authenticated;
