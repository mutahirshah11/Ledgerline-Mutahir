-- Behavioural smoke test of supabase/migrations/0001_init.sql on a real Postgres.
-- Run by supabase/tests/run_smoke.sh after supabase_stub.sql and the migration, with
--   psql -v ON_ERROR_STOP=1 -v payload_a="$(cat fixtures/payload_a.json)" -v payload_b=...
-- The payloads are ReconciliationPayload.model_dump(mode="json") (see make_payload.py).
-- Every failed assertion raises, so psql stops with a non-zero exit code.

\set ON_ERROR_STOP 1
set client_min_messages = notice;

-- =============================================================================================
-- Harness: fixtures and assertion helpers live in schema smoke (dropped at the end)
-- =============================================================================================
create schema smoke;
create table smoke.fixture (name text primary key, body jsonb not null);
insert into smoke.fixture (name, body) values ('a', :'payload_a'), ('b', :'payload_b');

create function smoke.fx(p_name text) returns jsonb
language sql stable as $$ select body from smoke.fixture where name = p_name $$;

-- the run every payload element belongs to
create function smoke.run_id() returns uuid
language sql stable as $$ select (smoke.fx('a') -> 'rows' -> 0 ->> 'run_id')::uuid $$;

create function smoke.other_run() returns uuid
language sql immutable as $$ select '00000000-0000-4000-8000-0000000000b2'::uuid $$;

create function smoke.finalised_run() returns uuid
language sql immutable as $$ select '00000000-0000-4000-8000-0000000000f1'::uuid $$;

create function smoke.eq(p_actual anycompatible, p_expected anycompatible, p_what text)
returns void language plpgsql as $$
begin
    if p_actual is distinct from p_expected then
        raise exception 'FAIL %: got %, expected %', p_what, p_actual, p_expected;
    end if;
    raise notice 'ok   %', p_what;
end;
$$;

-- Run p_sql (optionally as p_role) and require it to fail with p_state (and p_constraint).
-- Whatever p_sql changed is rolled back with its subtransaction.
create function smoke.expect_error(
    p_what text, p_sql text, p_state text,
    p_constraint text default null, p_role text default null, p_message_like text default null
) returns void language plpgsql as $$
declare
    v_state text;
    v_msg   text;
    v_con   text;
begin
    begin
        if p_role is not null then
            execute format('set local role %I', p_role);
        end if;
        execute p_sql;
        raise exception 'statement succeeded' using errcode = 'XX999';
    exception when others then
        get stacked diagnostics
            v_state = returned_sqlstate, v_msg = message_text, v_con = constraint_name;
    end;
    if v_state = 'XX999' then
        raise exception 'FAIL %: expected SQLSTATE %, but the statement succeeded', p_what, p_state;
    end if;
    if v_state <> p_state then
        raise exception 'FAIL %: expected SQLSTATE %, got % (%)', p_what, p_state, v_state, v_msg;
    end if;
    if p_constraint is not null and v_con is distinct from p_constraint then
        raise exception 'FAIL %: expected constraint %, got % (%)', p_what, p_constraint, v_con,
            v_msg;
    end if;
    if p_message_like is not null and v_msg not like p_message_like then
        raise exception 'FAIL %: message "%" is not like "%"', p_what, v_msg, p_message_like;
    end if;
    raise notice 'ok   % -> % %', p_what, v_state, coalesce('[' || v_con || '] ', '') || v_msg;
end;
$$;

-- replace_reconciliation exactly as the API calls it: as service_role
create function smoke.apply(p_run uuid, p_payload jsonb) returns void
language plpgsql as $$
begin
    set local role service_role;
    perform public.replace_reconciliation(p_run, p_payload);
    reset role;
end;
$$;

create function smoke.call_sql(p_run uuid, p_payload jsonb) returns text
language sql immutable as $$
    select format('select public.replace_reconciliation(%L::uuid, %L::jsonb)', p_run, p_payload)
$$;

-- fingerprint of everything replace_reconciliation owns for a run
create function smoke.state(p_run uuid) returns text
language sql stable as $$
    select md5(concat_ws('|',
        (select string_agg(concat_ws(',', id, source_row_id, qty, qty_base, group_id, outcome,
                                     row_kind, duplicate_of, meta::text), ';'
                           order by source_row_id)
           from public.source_rows where run_id = p_run),
        (select string_agg(concat_ws(',', id, group_key, outcome, missing_from::text,
                                     row_ids::text, diffs::text), ';' order by group_key)
           from public.groups where run_id = p_run),
        (select string_agg(concat_ws(',', id, flag, detail::text), ';' order by seq)
           from public.control_flags where run_id = p_run),
        (select string_agg(concat_ws(',', id, code, before, after), ';' order by seq)
           from public.normalisation_log where run_id = p_run)))
$$;

-- fingerprint of the tables replace_reconciliation must never touch
create function smoke.untouched(p_run uuid) returns text
language sql stable as $$
    select md5(concat_ws('|',
        (select string_agg(r::text, ';' order by r.id) from public.review_items r
          where r.run_id = p_run),
        (select string_agg(p::text, ';') from public.proofs p where p.run_id = p_run),
        (select string_agg(n::text, ';' order by n.seq) from public.notifications n
          where n.run_id = p_run),
        (select string_agg(e::text, ';' order by e.seq) from public.run_events e
          where e.run_id = p_run),
        (select string_agg(a::text, ';' order by a.id) from public.audit_log a
          where a.run_id = p_run)))
$$;

-- the stored run equals the payload, field by field
create function smoke.assert_matches(p_run uuid, p_payload jsonb, p_what text)
returns void language plpgsql as $$
declare
    v_n int;
begin
    perform smoke.eq(
        (select count(*)::int from public.source_rows where run_id = p_run),
        jsonb_array_length(p_payload -> 'rows'), p_what || ': row count');
    select count(*) into v_n
    from jsonb_array_elements(p_payload -> 'rows') as r (e)
    where not exists (
        select 1 from public.source_rows s
        where s.run_id = p_run
          and s.id = (e ->> 'id')::uuid
          and s.source_row_id = e ->> 'source_row_id'
          and s.source::text = e ->> 'source'
          and s.row_kind::text = e ->> 'row_kind'
          and s.grn_no is not distinct from (e ->> 'grn_no')::integer
          and s.date is not distinct from (e ->> 'date')::date
          and s.supplier_code is not distinct from e ->> 'supplier_code'
          and s.item is not distinct from e ->> 'item'
          and s.lot_no is not distinct from e ->> 'lot_no'
          and s.qty::text is not distinct from e ->> 'qty'        -- numeric keeps the scale
          and s.uom is not distinct from e ->> 'uom'
          and s.qty_base::text is not distinct from e ->> 'qty_base'
          and s.base_uom is not distinct from e ->> 'base_uom'
          and s.cell_status = e -> 'cell_status'
          and s.raw = e -> 'raw'
          and s.meta = e -> 'meta'
          and s.duplicate_of is not distinct from e ->> 'duplicate_of'
          and s.group_id is not distinct from (e ->> 'group_id')::uuid
          and s.outcome::text is not distinct from e ->> 'outcome');
    perform smoke.eq(v_n, 0, p_what || ': payload rows stored exactly');

    perform smoke.eq(
        (select count(*)::int from public.groups where run_id = p_run),
        jsonb_array_length(p_payload -> 'groups'), p_what || ': group count');
    select count(*) into v_n
    from jsonb_array_elements(p_payload -> 'groups') as g (e)
    where not exists (
        select 1 from public.groups s
        where s.run_id = p_run
          and s.id = (e ->> 'id')::uuid
          and s.group_key = e ->> 'group_key'
          and s.grn_no is not distinct from (e ->> 'grn_no')::integer
          and s.row_ids = e -> 'row_ids'
          and s.outcome::text = e ->> 'outcome'
          and s.missing_from::text[]
              = array(select jsonb_array_elements_text(e -> 'missing_from'))
          and s.diffs = e -> 'diffs');
    perform smoke.eq(v_n, 0, p_what || ': payload groups stored exactly');

    -- flags and norm_log: same elements, in payload order by seq
    perform smoke.eq(
        (select array_agg(row(id, flag::text, grn_no, source_row_id, detail)::text order by seq)
           from public.control_flags where run_id = p_run),
        (select array_agg(row((e ->> 'id')::uuid, e ->> 'flag', (e ->> 'grn_no')::integer,
                              e ->> 'source_row_id', e -> 'detail')::text order by o)
           from jsonb_array_elements(p_payload -> 'flags') with ordinality as f (e, o)),
        p_what || ': flags equal payload, in seq order');
    perform smoke.eq(
        (select array_agg(row(id, source_row_id, field, code::text, before, after)::text
                          order by seq)
           from public.normalisation_log where run_id = p_run),
        (select array_agg(row((e ->> 'id')::uuid, e ->> 'source_row_id', e ->> 'field',
                              e ->> 'code', e ->> 'before', e ->> 'after')::text order by o)
           from jsonb_array_elements(p_payload -> 'norm_log') with ordinality as n (e, o)),
        p_what || ': norm_log equals payload, in seq order');
end;
$$;


-- =============================================================================================
-- 1. Schema facts: 14 tables, RLS on, no policies, private buckets, no timestamp defaults
-- =============================================================================================
do $$
begin
    perform smoke.eq(
        (select count(*)::int from pg_tables where schemaname = 'public'), 14, '14 public tables');
    perform smoke.eq(
        (select count(*)::int from pg_class c join pg_namespace n on n.oid = c.relnamespace
          where n.nspname = 'public' and c.relkind = 'r' and c.relrowsecurity), 14,
        'RLS enabled on all 14 tables');
    perform smoke.eq(
        (select count(*)::int from pg_policies where schemaname = 'public'), 0, 'no policies');
    perform smoke.eq(
        (select count(*)::int from pg_type t join pg_namespace n on n.oid = t.typnamespace
          where n.nspname = 'public' and t.typtype = 'e'), 16, '16 enum types');
    perform smoke.eq(
        (select array_agg(id || ':' || public::text order by id) from storage.buckets),
        array['inputs:false', 'reports:false'], 'buckets inputs and reports are private');
    perform smoke.eq(
        (select count(*)::int from information_schema.columns
          where table_schema = 'public' and data_type in ('timestamp with time zone', 'date')
            and column_default is not null), 0, 'no timestamp/date column has a default');
    perform smoke.eq(
        (select count(*)::int from information_schema.columns
          where table_schema = 'public' and data_type in ('real', 'double precision')), 0,
        'no floating point columns');
end;
$$;


-- =============================================================================================
-- 2. Setup: three runs and some rows in the tables replace_reconciliation must not touch
-- =============================================================================================
insert into public.runs (id, month, status, scenario_now, created_by, created_at)
values
    (smoke.run_id(), '2026-09', 'EXTRACTED', '2026-10-01 09:00+05', 'controller@kestrel.example',
     '2026-10-01 09:00+05'),
    (smoke.other_run(), '2026-09', 'CREATED', '2026-10-01 09:00+05',
     'controller@kestrel.example', '2026-10-01 09:00+05');
insert into public.runs (id, month, status, run_key, scenario_now, created_by, created_at,
                         finalised_at, finalised_by)
values (smoke.finalised_run(), '2026-08', 'FINALISED', repeat('a', 64), '2026-09-05 09:00+05',
        'controller@kestrel.example', '2026-09-05 09:00+05', '2026-09-05 10:00+05',
        'controller@kestrel.example');

insert into public.review_items (run_id, kind, item_key, reason, context, status)
values (smoke.run_id(), 'CELL', 'CELL:REG-p2-r14:qty', 'CORRECTED',
        '{"page_no": 2, "row_index": 14}', 'OPEN');
insert into public.proofs (run_id, status, checks, computed_at)
values (smoke.run_id(), 'PASS', '{}', '2026-10-01 09:05+05');
insert into public.notifications (run_id, kind, idempotency_key, status, updated_at)
values (smoke.run_id(), 'ALERT', 'ALERT:smoke:extract:1', 'SENT', '2026-10-01 09:06+05');
insert into public.run_events (run_id, stage, status, at, detail)
values (smoke.run_id(), 'reconcile', 'OK', '2026-10-01 09:05+05', '{}');
insert into public.audit_log (at, actor, action, entity, entity_id, run_id, after, channel)
values ('2026-10-01 09:05+05', 'system', 'RECONCILED', 'run', smoke.run_id()::text,
        smoke.run_id(), '{"status": "READY"}', 'SYSTEM');

create table smoke.saved (name text primary key, value text);
insert into smoke.saved values ('untouched', smoke.untouched(smoke.run_id()));


-- =============================================================================================
-- 3. replace_reconciliation: first reconcile, re-reconcile, idempotent repeat
-- =============================================================================================
do $$
begin
    perform smoke.apply(smoke.run_id(), smoke.fx('a'));
    perform smoke.assert_matches(smoke.run_id(), smoke.fx('a'), 'payload A');
    perform smoke.eq(
        (select qty_base from public.source_rows where source_row_id = 'REG-p2-r15'),
        1097.28::numeric, 'A: converted qty_base is exact numeric');
    perform smoke.eq(
        (select missing_from from public.groups where group_key = 'GRN:4427'),
        array['SHEET']::source[], 'A: missing_from is a source[]');

    perform smoke.apply(smoke.run_id(), smoke.fx('b'));
    perform smoke.assert_matches(smoke.run_id(), smoke.fx('b'), 'payload B replaces A');
    perform smoke.eq(
        (select count(*)::int from public.source_rows
          where source_row_id in ('REG-p2-r16', 'ERP-0029', 'ERP-0030')), 0,
        'B: rows that left the payload are deleted');
    perform smoke.eq(
        (select count(*)::int from public.groups where group_key = 'GRN:4427'), 0,
        'B: group that left the payload is deleted');

    perform smoke.apply(smoke.run_id(), smoke.fx('b'));
    perform smoke.assert_matches(smoke.run_id(), smoke.fx('b'), 'payload B again (idempotent)');
end;
$$;

insert into smoke.saved values ('after_b', smoke.state(smoke.run_id()));


-- =============================================================================================
-- 4. Rejected payloads: right error, nothing written
-- =============================================================================================
do $$
declare
    b      jsonb := smoke.fx('b');
    run    uuid := smoke.run_id();
    row0   jsonb := smoke.fx('b') -> 'rows' -> 0;
    group0 jsonb := smoke.fx('b') -> 'groups' -> 0;
    n_rows int := jsonb_array_length(smoke.fx('b') -> 'rows');
begin
    -- duplicates inside one payload -> 23505 naming the constraint (never 21000)
    perform smoke.expect_error('duplicate source_row_id (same row twice)',
        smoke.call_sql(run, jsonb_set(b, '{rows}', (b -> 'rows') || jsonb_build_array(row0))),
        '23505', 'source_rows_run_row_uq', 'service_role');
    perform smoke.expect_error('duplicate source_row_id (different ids)',
        smoke.call_sql(run, jsonb_set(b, '{rows}', (b -> 'rows') || jsonb_build_array(
            jsonb_set(row0, '{id}', to_jsonb(gen_random_uuid()::text))))),
        '23505', 'source_rows_run_row_uq', 'service_role');
    perform smoke.expect_error('duplicate row id',
        smoke.call_sql(run, jsonb_set(b, '{rows}', (b -> 'rows') || jsonb_build_array(
            jsonb_set(jsonb_set(row0, '{source_row_id}', '"ERP-0999"'), '{group_id}',
                      'null')))),
        '23505', 'source_rows_pkey', 'service_role');
    perform smoke.expect_error('duplicate group_key',
        smoke.call_sql(run, jsonb_set(b, '{groups}', (b -> 'groups') || jsonb_build_array(
            jsonb_set(group0, '{id}', to_jsonb(gen_random_uuid()::text))))),
        '23505', 'groups_run_group_key_uq', 'service_role');
    perform smoke.expect_error('duplicate group id',
        smoke.call_sql(run, jsonb_set(b, '{groups}', (b -> 'groups') || jsonb_build_array(
            jsonb_set(group0, '{group_key}', '"GRN:9999"')))),
        '23505', 'groups_pkey', 'service_role');
    perform smoke.expect_error('duplicate flag id',
        smoke.call_sql(run, jsonb_set(b, '{flags}', (b -> 'flags') || (b -> 'flags'))),
        '23505', 'control_flags_pkey', 'service_role');
    perform smoke.expect_error('duplicate norm_log id',
        smoke.call_sql(run, jsonb_set(b, '{norm_log}',
            (b -> 'norm_log') || jsonb_build_array(b -> 'norm_log' -> 0))),
        '23505', 'normalisation_log_pkey', 'service_role');

    -- contract violations -> 22023 CONTRACT_VIOLATION (never 22P02 from a cast)
    perform smoke.expect_error('run_id is not a uuid',
        smoke.call_sql(run, jsonb_set(b, '{rows,0,run_id}', '"not-a-uuid"')),
        '22023', null, 'service_role', 'CONTRACT_VIOLATION%');
    perform smoke.expect_error('id is not a uuid',
        smoke.call_sql(run, jsonb_set(b, '{flags,0,id}', '"flag-1"')),
        '22023', null, 'service_role', 'CONTRACT_VIOLATION%');
    perform smoke.expect_error('element of another run',
        smoke.call_sql(run, jsonb_set(b, '{groups,0,run_id}', to_jsonb(smoke.other_run()))),
        '22023', null, 'service_role', 'CONTRACT_VIOLATION%');
    perform smoke.expect_error('row references a group not in the payload',
        smoke.call_sql(run, jsonb_set(b, '{rows,0,group_id}', to_jsonb(gen_random_uuid()))),
        '22023', null, 'service_role', 'CONTRACT_VIOLATION%group%');
    perform smoke.expect_error('quantity sent as a JSON number',
        smoke.call_sql(run, jsonb_set(b, '{rows,0,qty}', '40.5')),
        '22023', null, 'service_role', 'CONTRACT_VIOLATION%');
    perform smoke.expect_error('payload key missing',
        smoke.call_sql(run, b - 'flags'),
        '22023', null, 'service_role', 'CONTRACT_VIOLATION%');

    -- failure in the very last statement (bad enum in the last norm_log entry): all-or-nothing
    perform smoke.expect_error('failure on the last insert rolls everything back',
        smoke.call_sql(run, jsonb_set(smoke.fx('a'),
            array['norm_log', (jsonb_array_length(smoke.fx('a') -> 'norm_log') - 1)::text,
                  'code'], '"NOPE"')),
        '22P02', null, 'service_role');

    -- run state
    perform smoke.expect_error('FINALISED run is read-only',
        smoke.call_sql(smoke.finalised_run(), b),
        '55000', null, 'service_role', 'RUN_STATE_CONFLICT%');
    perform smoke.expect_error('unknown run',
        smoke.call_sql(gen_random_uuid(), b),
        'P0002', null, 'service_role', 'NOT_FOUND%');

    perform smoke.eq(smoke.state(run), (select value from smoke.saved where name = 'after_b'),
        'state after the rejected payloads is still exactly B');
    perform smoke.eq(n_rows, (select count(*)::int from public.source_rows where run_id = run),
        'row count unchanged');
end;
$$;


-- =============================================================================================
-- 5. Prune happens before the upsert: a new row may re-use the id of a row that left
-- =============================================================================================
do $$
declare
    run      uuid := smoke.run_id();
    a        jsonb := smoke.fx('a');
    freed_id text := (select id::text from public.source_rows where source_row_id = 'ERP-0031');
    idx      int;
    reuse    jsonb;
begin
    select o - 1 into idx
    from jsonb_array_elements(a -> 'rows') with ordinality as t (e, o)
    where e ->> 'source_row_id' = 'REG-p2-r16';
    reuse := jsonb_set(a, array['rows', idx::text, 'id'], to_jsonb(freed_id));
    perform smoke.apply(run, reuse);
    perform smoke.assert_matches(run, reuse, 'A with an id freed by a pruned row');
    perform smoke.apply(run, smoke.fx('b'));
    perform smoke.eq(smoke.state(run), (select value from smoke.saved where name = 'after_b'),
        'back to B');
end;
$$;


-- =============================================================================================
-- 6. Tables replace_reconciliation must never touch are unchanged
-- =============================================================================================
do $$
begin
    perform smoke.eq(smoke.untouched(smoke.run_id()),
        (select value from smoke.saved where name = 'untouched'),
        'review_items, proofs, notifications, run_events, audit_log untouched');
end;
$$;


-- =============================================================================================
-- 7. Constraints
-- =============================================================================================
do $$
declare
    run   uuid := smoke.run_id();
    other uuid := smoke.other_run();
    grp   uuid := (select id from public.groups where run_id = smoke.run_id() limit 1);
begin
    perform smoke.expect_error('FAILED run without failed_stage',
        $q$insert into public.runs (month, status, scenario_now, created_by, created_at)
           values ('2026-09', 'FAILED', now(), 'x', now())$q$,
        '23514', 'runs_failed_stage_iff_failed_ck');
    perform smoke.expect_error('failed_stage on a run that is not FAILED',
        $q$insert into public.runs (month, status, failed_stage, scenario_now, created_by,
                                    created_at)
           values ('2026-09', 'CREATED', 'extract', now(), 'x', now())$q$,
        '23514', 'runs_failed_stage_iff_failed_ck');
    perform smoke.expect_error('unknown stage name',
        $q$insert into public.runs (month, status, failed_stage, scenario_now, created_by,
                                    created_at)
           values ('2026-09', 'FAILED', 'Extract', now(), 'x', now())$q$,
        '23514', 'runs_failed_stage_ck');
    perform smoke.expect_error('bad month',
        $q$insert into public.runs (month, status, scenario_now, created_by, created_at)
           values ('2026-13', 'CREATED', now(), 'x', now())$q$,
        '23514', 'runs_month_ck');
    perform smoke.expect_error('CANNOT_DETERMINE with a blank note',
        format($q$insert into public.review_items (run_id, kind, item_key, reason, context,
                                                   status, note)
                  values (%L, 'CELL', 'CELL:x:qty', 'ILLEGIBLE', '{}', 'CANNOT_DETERMINE', '   ')
               $q$, run),
        '23514', 'review_items_cannot_determine_note_ck');
    perform smoke.expect_error('upper-case allowlist email',
        $q$insert into public.allowed_users values ('Ops@Kestrel.example', 'Ops', 'OPS')$q$,
        '23514', 'allowed_users_email_lower_ck');
    perform smoke.expect_error('second FINALISED run with the same run_key',
        format($q$insert into public.runs (month, status, run_key, scenario_now, created_by,
                                          created_at, finalised_at, finalised_by)
                  values ('2026-08', 'FINALISED', %L, now(), 'x', now(), now(), 'x')$q$,
               repeat('a', 64)),
        '23505', 'runs_run_key_finalised_uq');
    -- an unfinished re-run of the same inputs is allowed (C7)
    insert into public.runs (month, status, run_key, scenario_now, created_by, created_at)
    values ('2026-08', 'CREATED', repeat('a', 64), now(), 'x', now());
    raise notice 'ok   unfinished run may share a FINALISED run_key';

    perform smoke.expect_error('row cannot join a group of another run',
        format($q$insert into public.source_rows (run_id, source, source_row_id, row_kind,
                                                  cell_status, raw, meta, group_id)
                  values (%L, 'ERP', 'ERP-0001', 'RECEIPT', '{}', '{}', '{}', %L)$q$,
               other, grp)
        || '; set constraints all immediate',
        '23503', 'source_rows_group_fk');
end;
$$;


-- =============================================================================================
-- 8. audit_log is append-only, even for the service role and the owner
-- =============================================================================================
do $$
begin
    perform smoke.expect_error('UPDATE audit_log (owner)',
        $q$update public.audit_log set actor = 'mallory'$q$, '42501', null, null,
        'audit_log is append-only');
    perform smoke.expect_error('DELETE audit_log (owner)',
        $q$delete from public.audit_log$q$, '42501', null, null, 'audit_log is append-only');
    perform smoke.expect_error('TRUNCATE audit_log (owner)',
        $q$truncate public.audit_log$q$, '42501', null, null, 'audit_log is append-only');
    perform smoke.expect_error('UPDATE audit_log (service_role)',
        $q$update public.audit_log set actor = 'mallory'$q$, '42501', null, 'service_role',
        'audit_log is append-only');
    perform smoke.expect_error('DELETE audit_log (service_role)',
        $q$delete from public.audit_log$q$, '42501', null, 'service_role',
        'audit_log is append-only');
    perform smoke.eq((select count(*)::int from public.audit_log), 1, 'audit row still there');
end;
$$;


-- =============================================================================================
-- 9. Browser roles have no access; only service_role may call replace_reconciliation
-- =============================================================================================
do $$
declare
    t     text;
    r     text;
    privs constant text := 'select, insert, update, delete, truncate, references, trigger';
begin
    foreach r in array array['anon', 'authenticated'] loop
        foreach t in array array[
            'allowed_users', 'runs', 'source_files', 'page_manifest', 'extractions', 'groups',
            'source_rows', 'normalisation_log', 'control_flags', 'review_items', 'proofs',
            'notifications', 'run_events', 'audit_log'
        ] loop
            if has_table_privilege(r, 'public.' || t, privs) then
                raise exception 'FAIL % has a privilege on %', r, t;
            end if;
        end loop;
        raise notice 'ok   % has no privilege on any of the 14 tables', r;
        perform smoke.eq(
            has_sequence_privilege(r, 'public.audit_log_id_seq', 'usage, select, update'),
            false, r || ' has no privilege on audit_log_id_seq');
        perform smoke.eq(
            has_function_privilege(r, 'public.replace_reconciliation(uuid, jsonb)', 'execute'),
            false, r || ' cannot execute replace_reconciliation');
    end loop;
    perform smoke.eq(
        has_function_privilege('service_role', 'public.replace_reconciliation(uuid, jsonb)',
                               'execute'),
        true, 'service_role can execute replace_reconciliation');

    perform smoke.expect_error('anon reads runs', 'select count(*) from public.runs',
        '42501', null, 'anon');
    perform smoke.expect_error('authenticated reads audit_log',
        'select count(*) from public.audit_log', '42501', null, 'authenticated');
    perform smoke.expect_error('anon calls replace_reconciliation',
        smoke.call_sql(smoke.run_id(), smoke.fx('b')), '42501', null, 'anon');
    perform smoke.expect_error('authenticated calls replace_reconciliation',
        smoke.call_sql(smoke.run_id(), smoke.fx('b')), '42501', null, 'authenticated');
end;
$$;

-- objects created later in public are not granted to the browser roles (default privileges)
create table public.smoke_probe (x int);
create sequence public.smoke_probe_seq;
do $$
begin
    perform smoke.eq(
        has_table_privilege('anon', 'public.smoke_probe', 'select, insert, update, delete'),
        false, 'a table created later is not granted to anon');
    perform smoke.eq(
        has_table_privilege('authenticated', 'public.smoke_probe',
                            'select, insert, update, delete'),
        false, 'a table created later is not granted to authenticated');
    perform smoke.eq(
        has_sequence_privilege('anon', 'public.smoke_probe_seq', 'usage, select, update'),
        false, 'a sequence created later is not granted to anon');
    perform smoke.eq(
        has_table_privilege('service_role', 'public.smoke_probe', 'select'),
        true, 'a table created later is still granted to service_role');
end;
$$;
drop table public.smoke_probe;
drop sequence public.smoke_probe_seq;

-- functions created later: Postgres grants EXECUTE to PUBLIC by default, which would let anon
-- call them (e.g. as a PostgREST RPC); only service_role may execute them
create function public.smoke_probe_fn() returns int language sql as $$ select 1 $$;
create function public.smoke_probe_definer_fn() returns int
    language sql security definer as $$ select count(*)::int from public.runs $$;
do $$
declare
    fn text;
    r  text;
begin
    foreach fn in array array['public.smoke_probe_fn()', 'public.smoke_probe_definer_fn()'] loop
        foreach r in array array['anon', 'authenticated'] loop
            perform smoke.eq(has_function_privilege(r, fn, 'execute'), false,
                format('a function created later (%s) is not executable by %s', fn, r));
            perform smoke.expect_error(format('%s calls %s', r, fn), 'select ' || fn,
                '42501', null, r);
        end loop;
        perform smoke.eq(has_function_privilege('service_role', fn, 'execute'), true,
            format('a function created later (%s) is executable by service_role', fn));
        set local role service_role;
        execute 'select ' || fn;
        reset role;
        raise notice 'ok   service_role calls %', fn;
    end loop;
end;
$$;
drop function public.smoke_probe_fn();
drop function public.smoke_probe_definer_fn();


-- =============================================================================================
-- 10. 0003: no review decision on a FINALISED run (F-08.R8)
-- =============================================================================================
insert into public.review_items (run_id, kind, item_key, reason, context, status)
values (smoke.finalised_run(), 'CELL', 'CELL:REG-p1-r01:qty', 'CORRECTED',
        '{"page_no": 1, "row_index": 1}', 'OPEN');
do $$
begin
    perform smoke.expect_error('decision on a FINALISED run',
        format($q$update public.review_items set status = 'RESOLVED', resolved_value = '12',
                  resolved_by = 'reviewer@kestrel.example', resolved_at = now()
                  where run_id = %L$q$, smoke.finalised_run()),
        '55000', null, 'service_role', 'RUN_STATE_CONFLICT%');
    update public.review_items set note = note where run_id = smoke.run_id();
    raise notice 'ok   review items of an unfinalised run can be updated';
end;
$$;

-- the upsert_review_items refresh (ON CONFLICT DO UPDATE) is refused on a FINALISED run too
do $$
begin
    perform smoke.expect_error('upsert refresh on a FINALISED run',
        format($q$select count(*) from public.upsert_review_items(%L, %L::jsonb)$q$,
               smoke.finalised_run(),
               jsonb_build_array(jsonb_build_object(
                   'id', gen_random_uuid(), 'run_id', smoke.finalised_run(), 'kind', 'CELL',
                   'item_key', 'CELL:REG-p1-r01:qty', 'reason', 'CORRECTED',
                   'context', '{"page_no": 1, "row_index": 1}'::jsonb))),
        '55000', null, 'service_role', 'RUN_STATE_CONFLICT%');
end;
$$;

-- 0003 runs_refuse_unreconciled_finalise: no FINALISED while a decision is not reconciled
insert into public.runs (id, month, status, run_key, scenario_now, created_by, created_at)
values ('00000000-0000-4000-8000-0000000000f3', '2026-07', 'READY', repeat('c', 64),
        '2026-10-01 09:00+05', 'controller@kestrel.example', '2026-10-01 09:00+05');
insert into public.review_items (run_id, kind, item_key, reason, context, status,
                                 resolved_value, resolved_by, resolved_at)
values ('00000000-0000-4000-8000-0000000000f3', 'CELL', 'CELL:REG-p1-r02:qty', 'CORRECTED',
        '{"page_no": 1, "row_index": 2}', 'RESOLVED', '12', 'reviewer@kestrel.example',
        '2026-10-01 09:10+05'),
       ('00000000-0000-4000-8000-0000000000f3', 'CELL', 'CELL:REG-p1-r03:qty', 'CORRECTED',
        '{"page_no": 1, "row_index": 3}', 'OPEN', null, null, null);
do $$
declare
    run  constant uuid := '00000000-0000-4000-8000-0000000000f3';
    fin  constant text := format(
        $q$update public.runs set status = 'FINALISED', finalised_at = now(),
              finalised_by = 'controller@kestrel.example' where id = %L$q$,
        '00000000-0000-4000-8000-0000000000f3');
begin
    perform smoke.expect_error('FINALISED with an unreconciled decision (no reconcile yet)',
        fin, '55000', null, 'service_role', 'RUN_STATE_CONFLICT%');
    insert into public.audit_log (at, actor, action, entity, entity_id, run_id, after, channel)
    values ('2026-10-01 09:05+05', 'system', 'RECONCILED', 'run', run::text, run,
            '{"status": "READY", "open_item_keys": []}', 'SYSTEM');
    perform smoke.expect_error('FINALISED with an unreconciled decision (legacy marker)',
        fin, '55000', null, 'service_role', 'RUN_STATE_CONFLICT%');
    insert into public.audit_log (at, actor, action, entity, entity_id, run_id, after, channel)
    values ('2026-10-01 09:20+05', 'system', 'RECONCILED', 'run', run::text, run,
            '{"decided_item_keys": ["CELL:REG-p1-r02:qty"]}', 'SYSTEM'),
           -- same run, older `at` but newer id: a stale reconcile written last wins
           ('2026-10-01 09:15+05', 'system', 'RECONCILED', 'run', run::text, run,
            '{"decided_item_keys": []}', 'SYSTEM');
    perform smoke.expect_error('FINALISED with an unreconciled decision (newest by id)',
        fin, '55000', null, 'service_role', 'RUN_STATE_CONFLICT%');
    insert into public.audit_log (at, actor, action, entity, entity_id, run_id, after, channel)
    values ('2026-10-01 09:15+05', 'system', 'RECONCILED', 'run', run::text, run,
            '{"decided_item_keys": ["CELL:REG-p1-r02:qty"]}', 'SYSTEM');
    set local role service_role;
    execute fin;
    reset role;
    perform smoke.eq((select status::text from public.runs where id = run), 'FINALISED',
        'a run whose decisions are all reconciled is finalised');
end;
$$;


-- =============================================================================================
-- Done
-- =============================================================================================
drop schema smoke cascade;
do $$ begin raise notice 'SMOKE PASSED'; end; $$;
