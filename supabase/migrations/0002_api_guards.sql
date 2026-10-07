-- =============================================================================================
-- Ledgerline 0002_api_guards.sql: database guards for SupabaseRepo (Phase 3, research R12).
-- Additive only: one RPC, two trigger functions, two triggers. Apply after 0001_init.sql.
--
--   1. upsert_review_items(p_run_id, p_items): Repo.upsert_review_items in one transaction.
--      New item_keys are inserted OPEN; existing ones get only the refreshed columns (reason,
--      read_a, read_b, context, grn_no, source_row_id, field), so a decision made in between
--      is never overwritten (F-08.R1).
--   2. proofs: no insert or update while the run is FINALISED (Repo.save_proof).
--   3. source_files: no delete unless the run is CREATED (Repo.delete_source_file, FM-02).
--
-- Errors (message prefix = API error code), mapped by SupabaseRepo:
--   NOT_FOUND           (P0002) run does not exist                  → NotFound
--   RUN_STATE_CONFLICT  (55000) run state forbids the write         → StateConflict(run)
--   CONTRACT_VIOLATION  (22023) p_items is not an array of objects with this run's uuid ids
--                                                                    → ValueError
--   UNIQUE_VIOLATION    (23505) an item_key repeats in p_items, or an id is already used
--                                                                    → UniqueViolation
-- Postgres 17 (Supabase).
-- =============================================================================================


-- ---------------------------------------------------------------------------------------------
-- 1. upsert_review_items
-- ---------------------------------------------------------------------------------------------
create or replace function upsert_review_items(p_run_id uuid, p_items jsonb)
returns setof review_items
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
declare
    c_uuid  constant text :=
        '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
    v_bad   text;
begin
    -- the run must exist; share-lock it so it cannot change state during the call
    perform 1 from runs r where r.id = p_run_id for share;
    if not found then
        raise exception 'NOT_FOUND: run % does not exist', p_run_id
            using errcode = 'no_data_found';
    end if;

    if jsonb_typeof(p_items) is distinct from 'array' then
        raise exception 'CONTRACT_VIOLATION: p_items must be an array'
            using errcode = 'invalid_parameter_value';
    end if;
    select e.value::text into v_bad
    from jsonb_array_elements(p_items) as e
    where case
        when jsonb_typeof(e.value) <> 'object' then true
        when coalesce(e.value ->> 'id', '') !~* c_uuid then true
        when coalesce(e.value ->> 'run_id', '') !~* c_uuid then true
        else (e.value ->> 'run_id')::uuid <> p_run_id
    end
    limit 1;
    if v_bad is not null then
        raise exception 'CONTRACT_VIOLATION: item has a bad id or run_id: %', left(v_bad, 200)
            using errcode = 'invalid_parameter_value';
    end if;

    -- a repeated item_key would make the upsert fail with 21000; report the constraint instead
    select e.value ->> 'item_key' into v_bad
    from jsonb_array_elements(p_items) as e
    group by e.value ->> 'item_key'
    having count(*) > 1
    limit 1;
    if v_bad is not null then
        raise exception 'UNIQUE_VIOLATION: % appears more than once (review_items_run_item_key_uq)',
            v_bad
            using errcode = 'unique_violation', constraint = 'review_items_run_item_key_uq';
    end if;

    insert into review_items (
        id, run_id, kind, item_key, grn_no, source_row_id, field, reason, read_a, read_b,
        context, status, resolved_value, resolved_by, resolved_at, note
    )
    select
        (i.elem ->> 'id')::uuid,
        p_run_id,
        (i.elem ->> 'kind')::review_kind,
        i.elem ->> 'item_key',
        (i.elem ->> 'grn_no')::integer,
        i.elem ->> 'source_row_id',
        i.elem ->> 'field',
        i.elem ->> 'reason',
        nullif(i.elem -> 'read_a', 'null'::jsonb),
        nullif(i.elem -> 'read_b', 'null'::jsonb),
        i.elem -> 'context',
        'OPEN',
        null,
        null,
        null,
        null
    from jsonb_array_elements(p_items) with ordinality as i (elem, ord)
    order by i.ord
    on conflict (run_id, item_key) do update set
        reason        = excluded.reason,
        read_a        = excluded.read_a,
        read_b        = excluded.read_b,
        context       = excluded.context,
        grn_no        = excluded.grn_no,
        source_row_id = excluded.source_row_id,
        field         = excluded.field;

    return query
    select ri.*
    from review_items ri
    where ri.run_id = p_run_id
      and ri.item_key in (select e.value ->> 'item_key' from jsonb_array_elements(p_items) as e);
end;
$$;

-- only the API (service_role) may call it
revoke all on function upsert_review_items(uuid, jsonb) from public, anon, authenticated;
grant execute on function upsert_review_items(uuid, jsonb) to service_role;


-- ---------------------------------------------------------------------------------------------
-- 2. proofs: a FINALISED run is read-only (Repo.save_proof raises StateConflict)
-- ---------------------------------------------------------------------------------------------
create or replace function proofs_refuse_finalised_run()
returns trigger
language plpgsql
set search_path = public, pg_temp
as $$
declare
    v_status run_status;
begin
    -- share-lock the run row first, whatever its status (a status filter in this query would
    -- skip the lock exactly when the write is allowed); a concurrent finalise then waits for
    -- this transaction, and the status read below is the one that holds until commit
    select r.status into v_status from runs r where r.id = new.run_id for share;
    if v_status = 'FINALISED' then
        raise exception 'RUN_STATE_CONFLICT: run % is FINALISED', new.run_id
            using errcode = 'object_not_in_prerequisite_state';
    end if;
    return new;
end;
$$;

create trigger proofs_refuse_finalised_run
    before insert or update on proofs
    for each row execute function proofs_refuse_finalised_run();


-- ---------------------------------------------------------------------------------------------
-- 3. source_files: files are removed only while the run is CREATED (FM-02)
-- ---------------------------------------------------------------------------------------------
create or replace function source_files_delete_only_created()
returns trigger
language plpgsql
set search_path = public, pg_temp
as $$
declare
    v_status run_status;
begin
    -- lock first, then check (see proofs_refuse_finalised_run)
    select r.status into v_status from runs r where r.id = old.run_id for share;
    if v_status is distinct from 'CREATED' then
        raise exception 'RUN_STATE_CONFLICT: run % is not CREATED', old.run_id
            using errcode = 'object_not_in_prerequisite_state';
    end if;
    return old;
end;
$$;

create trigger source_files_delete_only_created
    before delete on source_files
    for each row execute function source_files_delete_only_created();

revoke all on function proofs_refuse_finalised_run() from public, anon, authenticated;
revoke all on function source_files_delete_only_created() from public, anon, authenticated;
