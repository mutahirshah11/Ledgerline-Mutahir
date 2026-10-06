-- =============================================================================================
-- Ledgerline 0001_init.sql: the whole Phase 0 schema in one migration.
--
-- Contract:  specs/001-foundation/contracts/migration-0001.md (items 1 to 8)
-- Columns:   specs/001-foundation/data-model.md Section 5 (authoritative), enums Section 2
-- Blueprint: docs/blueprint.md Section 10, change log C7 (partial unique run_key),
--            C8 (duplicate_of, groups.row_ids, notifications.updated_at, audit_log.run_id),
--            C9 (stage names are lower-case text with a CHECK, not an enum)
--
-- Rules (constitution):
--   * quantities are numeric, never floating point;
--   * no timestamp column has a DB-side default: the app passes scenario-clock time;
--   * RLS is enabled on every table with NO policies; only the service role reads/writes;
--   * audit_log is append-only (trigger);
--   * storage buckets are private.
-- Columns marked "null" in data-model.md are nullable; every other column is NOT NULL.
-- All foreign keys are ON DELETE RESTRICT.
-- Postgres 17 (Supabase).
-- =============================================================================================


-- ---------------------------------------------------------------------------------------------
-- 1. Extensions
-- ---------------------------------------------------------------------------------------------
-- pgcrypto provides gen_random_uuid() for id columns (ids only; no timestamp defaults).
create extension if not exists pgcrypto;


-- ---------------------------------------------------------------------------------------------
-- 2. Enum types (16), values exactly as data-model.md Section 2.1 and 2.2
--    Section 2.3 code lists (Stage, ReviewReason, AuditAction, ...) are stored as text.
-- ---------------------------------------------------------------------------------------------
create type source as enum ('REGISTER', 'ERP', 'SHEET');

create type cell_status as enum ('CLEAR', 'UNCLEAR', 'CORRECTED', 'ILLEGIBLE');

create type row_kind as enum ('RECEIPT', 'DUPLICATE', 'NON_RECEIPT');

create type outcome as enum (
    'MATCHED', 'MATCHED_AFTER_CONVERSION', 'MISMATCHED', 'MISSING', 'NEEDS_REVIEW',
    'UNMATCHED_NO_KEY', 'DUPLICATE', 'NON_RECEIPT'
);

create type flag_type as enum (
    'SEQUENCE_GAP', 'BACKDATED_ENTRY', 'UNUSUAL_USER', 'SUNDAY_DATE', 'OUT_OF_ORDER',
    'DATE_OUT_OF_PERIOD', 'UNKNOWN_SUPPLIER', 'UNKNOWN_UNIT'
);

create type review_kind as enum ('CELL', 'UNMATCHED_ROW', 'UNKNOWN_MAPPING', 'FLAG_ACK');

create type review_status as enum ('OPEN', 'RESOLVED', 'CANNOT_DETERMINE');

create type run_status as enum (
    'CREATED', 'UPLOADED', 'VALIDATED', 'EXTRACTING', 'EXTRACTED', 'RECONCILED',
    'AWAITING_REVIEW', 'READY', 'FINALISED', 'FAILED'
);

create type proof_status as enum ('PASS', 'FAIL');

create type notification_kind as enum ('SUMMARY', 'ALERT');

create type notification_status as enum ('PENDING', 'SENDING', 'SENT', 'FAILED');

create type norm_code as enum (
    'GRN_PREFIX_STRIPPED', 'DATE_SERIAL_PARSED', 'DATE_TEXT_PARSED', 'DATE_YEAR_FROM_HEADER',
    'SUPPLIER_MAPPED', 'UNIT_MAPPED', 'UNIT_CONVERTED', 'TEXT_TRIMMED'
);

create type role as enum ('CONTROLLER', 'REVIEWER', 'OPS');

create type audit_channel as enum ('WEB', 'API', 'SYSTEM', 'TELEGRAM');

create type run_event_status as enum ('STARTED', 'OK', 'RETRY', 'FAILED');

create type read_variant as enum ('A', 'B');


-- ---------------------------------------------------------------------------------------------
-- 3. Tables (14), in foreign-key order
-- ---------------------------------------------------------------------------------------------

-- T-USERS: allowlist for F-12
create table allowed_users (
    email         text primary key,
    display_name  text not null,
    role          role not null,
    constraint allowed_users_email_lower_ck check (email = lower(email))
);

-- T-RUNS: one row per run
create table runs (
    id             uuid primary key default gen_random_uuid(),
    month          text not null,
    status         run_status not null,
    run_key        text,
    failed_stage   text,
    error_message  text,
    scenario_now   timestamptz not null,
    created_by     text not null,
    created_at     timestamptz not null,
    finalised_at   timestamptz,
    finalised_by   text,
    constraint runs_month_ck check (month ~ '^\d{4}-(0[1-9]|1[0-2])$'),
    constraint runs_failed_stage_ck check (
        failed_stage is null or failed_stage in (
            'upload', 'validate', 'extract', 'reconcile', 'proof', 'review', 'finalise', 'notify'
        )
    ),
    -- a FAILED run always says which stage failed, and only a FAILED run does
    constraint runs_failed_stage_iff_failed_ck check ((status = 'FAILED') = (failed_stage is not null))
);

-- T-FILES: uploaded source files
-- seq (here and on control_flags, normalisation_log, notifications, run_events) is the
-- insertion order that Repo list methods return (repo-interface.md G3).
create table source_files (
    id            uuid primary key default gen_random_uuid(),
    seq           bigint not null generated always as identity,
    run_id        uuid not null references runs (id) on delete restrict,
    source        source not null,
    filename      text not null,
    storage_path  text not null,
    sha256        text not null,
    size_bytes    bigint not null,
    page_no       integer,
    confirmed_at  timestamptz,
    constraint source_files_run_sha256_uq unique (run_id, sha256),
    constraint source_files_page_no_ck check (page_no is null or source = 'REGISTER')
);

-- T-PAGES: drives "only new pages"
create table page_manifest (
    sha256             text primary key,
    month              text not null,
    page_no            integer not null,
    first_seen_run_id  uuid not null references runs (id) on delete restrict,
    first_seen_at      timestamptz not null
);

-- T-EXTRACT: the extraction cache
create table extractions (
    id              uuid primary key default gen_random_uuid(),
    page_sha256     text not null,
    read_variant    read_variant not null,
    model           text not null,
    prompt_version  text not null,
    result          jsonb not null,
    input_tokens    integer not null,
    output_tokens   integer not null,
    latency_ms      integer not null,
    created_at      timestamptz not null,
    constraint extractions_cache_uq unique (page_sha256, read_variant, model, prompt_version)
);

-- T-GROUPS: rebuilt on every reconcile
create table groups (
    id            uuid primary key default gen_random_uuid(),
    run_id        uuid not null references runs (id) on delete restrict,
    group_key     text not null,
    grn_no        integer,
    row_ids       jsonb not null,
    outcome       outcome not null,
    missing_from  source[] not null,
    diffs         jsonb not null,
    constraint groups_run_group_key_uq unique (run_id, group_key),
    -- target of the composite FK from source_rows: a row can only join a group of its own run
    constraint groups_run_id_id_uq unique (run_id, id)
);

-- T-ROWS: canonical rows of all three sources
create table source_rows (
    id             uuid primary key default gen_random_uuid(),
    run_id         uuid not null references runs (id) on delete restrict,
    source         source not null,
    source_row_id  text not null,
    row_kind       row_kind not null,
    grn_no         integer,
    date           date,
    supplier_code  text,
    item           text,
    lot_no         text,
    qty            numeric,
    uom            text,
    qty_base       numeric,
    base_uom       text,
    cell_status    jsonb not null,
    raw            jsonb not null,
    meta           jsonb not null,
    duplicate_of   text,
    group_id       uuid,
    outcome        outcome,
    constraint source_rows_run_row_uq unique (run_id, source_row_id),
    constraint source_rows_group_fk foreign key (run_id, group_id)
        references groups (run_id, id) on delete restrict deferrable initially deferred
);

-- T-NORMLOG: one row per changed value
create table normalisation_log (
    id             uuid primary key default gen_random_uuid(),
    seq            bigint not null generated always as identity,
    run_id         uuid not null references runs (id) on delete restrict,
    source_row_id  text not null,
    field          text not null,
    code           norm_code not null,
    before         text,
    after          text
);

-- T-FLAGS: rebuilt on every reconcile
create table control_flags (
    id             uuid primary key default gen_random_uuid(),
    seq            bigint not null generated always as identity,
    run_id         uuid not null references runs (id) on delete restrict,
    flag           flag_type not null,
    grn_no         integer,
    source_row_id  text,
    detail         jsonb not null
);

-- T-REVIEW: decisions survive re-reconcile
create table review_items (
    id              uuid primary key default gen_random_uuid(),
    run_id          uuid not null references runs (id) on delete restrict,
    kind            review_kind not null,
    item_key        text not null,
    grn_no          integer,
    source_row_id   text,
    field           text,
    reason          text not null,
    read_a          jsonb,
    read_b          jsonb,
    context         jsonb not null,
    status          review_status not null,
    resolved_value  text,
    resolved_by     text,
    resolved_at     timestamptz,
    note            text,
    constraint review_items_run_item_key_uq unique (run_id, item_key),
    constraint review_items_cannot_determine_note_ck check (
        status <> 'CANNOT_DETERMINE' or nullif(btrim(note), '') is not null
    )
);

-- T-PROOFS: overwritten each reconcile
create table proofs (
    run_id       uuid primary key references runs (id) on delete restrict,
    status       proof_status not null,
    checks       jsonb not null,
    computed_at  timestamptz not null
);

-- T-NOTIFS: the unique key blocks duplicate sends
create table notifications (
    id                   uuid primary key default gen_random_uuid(),
    seq                  bigint not null generated always as identity,
    run_id               uuid not null references runs (id) on delete restrict,
    kind                 notification_kind not null,
    idempotency_key      text not null,
    status               notification_status not null,
    attempts             integer not null default 0,
    last_error           text,
    telegram_message_id  bigint,
    sent_at              timestamptz,
    updated_at           timestamptz not null,
    constraint notifications_idempotency_key_uq unique (idempotency_key)
);

-- T-RUNLOG: one row per stage step
create table run_events (
    id           uuid primary key default gen_random_uuid(),
    seq          bigint not null generated always as identity,
    run_id       uuid not null references runs (id) on delete restrict,
    stage        text not null,
    status       run_event_status not null,
    at           timestamptz not null,
    duration_ms  integer,
    detail       jsonb not null,
    constraint run_events_stage_ck check (
        stage in (
            'upload', 'validate', 'extract', 'reconcile', 'proof', 'review', 'finalise', 'notify'
        )
    )
);

-- T-AUDIT: append-only (trigger in section 6); run_id is nullable and has no FK (C8)
create table audit_log (
    id         bigserial primary key,
    at         timestamptz not null,
    actor      text not null,
    action     text not null,
    entity     text not null,
    entity_id  text not null,
    run_id     uuid,
    before     jsonb,
    after      jsonb,
    channel    audit_channel not null
);


-- ---------------------------------------------------------------------------------------------
-- 4. Indexes
-- ---------------------------------------------------------------------------------------------
-- run_key is unique only among FINALISED runs, so unfinished inputs can be re-run (C7, issue B1)
create unique index runs_run_key_finalised_uq on runs (run_key) where status = 'FINALISED';

create index audit_log_run_id_idx on audit_log (run_id);

create index source_rows_run_source_row_idx on source_rows (run_id, source, source_row_id);

-- per-run lists, read in seq (insertion) order or, for run events, by time
create index normalisation_log_run_id_idx on normalisation_log (run_id, seq);
create index control_flags_run_id_idx on control_flags (run_id, seq);
create index notifications_run_id_idx on notifications (run_id, seq);
create index run_events_run_id_idx on run_events (run_id, at);


-- ---------------------------------------------------------------------------------------------
-- 5. Row level security: enabled everywhere, NO policies (only service_role, which bypasses
--    RLS, can read or write). Table privileges are also revoked from the browser roles.
-- ---------------------------------------------------------------------------------------------
alter table allowed_users enable row level security;
alter table runs enable row level security;
alter table source_files enable row level security;
alter table page_manifest enable row level security;
alter table extractions enable row level security;
alter table groups enable row level security;
alter table source_rows enable row level security;
alter table normalisation_log enable row level security;
alter table control_flags enable row level security;
alter table review_items enable row level security;
alter table proofs enable row level security;
alter table notifications enable row level security;
alter table run_events enable row level security;
alter table audit_log enable row level security;

revoke all on all tables in schema public from anon, authenticated;
revoke all on all sequences in schema public from anon, authenticated;

-- objects created later in public (by this role) are not granted to the browser roles either
alter default privileges in schema public revoke all on tables from anon, authenticated;
alter default privileges in schema public revoke all on sequences from anon, authenticated;
alter default privileges in schema public revoke all on functions from anon, authenticated;
-- Postgres grants EXECUTE on every new function to PUBLIC (hence to anon) by a built-in
-- default, and a per-schema default can only ADD to the global ones, so this revoke has
-- to be global (no IN SCHEMA). It applies to functions this role creates from now on, in
-- any schema; pgcrypto (section 1) is already installed. Functions in public that the API
-- needs are executable by service_role only.
alter default privileges revoke execute on functions from public;
alter default privileges in schema public grant execute on functions to service_role;


-- ---------------------------------------------------------------------------------------------
-- 6. Audit log is append-only (F-11.R6): UPDATE, DELETE and TRUNCATE all raise.
-- ---------------------------------------------------------------------------------------------
create or replace function audit_log_append_only()
returns trigger
language plpgsql
set search_path = public, pg_temp
as $$
begin
    raise exception 'audit_log is append-only'
        using errcode = 'insufficient_privilege',
              hint = 'audit_log rows can only be inserted';
end;
$$;

create trigger audit_log_no_update_delete
    before update or delete on audit_log
    for each row execute function audit_log_append_only();

create trigger audit_log_no_truncate
    before truncate on audit_log
    for each statement execute function audit_log_append_only();


-- ---------------------------------------------------------------------------------------------
-- 7. replace_reconciliation(p_run_id, p_payload): atomically rebuild a run's reconcile outputs
--    (contract item 7; Repo.replace_reconciliation, data-model.md Section 7).
--
--    Payload: {"rows": [CanonicalRow], "groups": [Group], "flags": [ControlFlag],
--              "norm_log": [NormLogEntry]} as produced by ReconciliationPayload
--              .model_dump(mode="json"); quantities are JSON strings, cast to numeric.
--    One function call = one transaction: any error aborts every change.
--    Touches only control_flags, normalisation_log, groups and source_rows; human decisions,
--    proofs, notifications, run events and the audit trail are left alone.
--    Every check runs before the first write. Errors (message prefix = API error code):
--      NOT_FOUND           (P0002) run does not exist
--      RUN_STATE_CONFLICT  (55000) run is FINALISED (read-only)
--      CONTRACT_VIOLATION  (22023) payload shape is wrong: a key is not an array, an element is
--                          not an object, an id / run_id is not a uuid string, an element
--                          belongs to another run, a quantity is a JSON number, or a row
--                          references a group that is not in the payload
--      UNIQUE_VIOLATION    (23505) a key appears twice in the payload; CONSTRAINT names the
--                          unique constraint the duplicate would break
-- ---------------------------------------------------------------------------------------------
create or replace function replace_reconciliation(p_run_id uuid, p_payload jsonb)
returns void
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
declare
    c_uuid       constant text :=
        '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
    v_status     run_status;
    v_key        text;
    v_bad        text;
    v_constraint text;
begin
    -- the run must exist and must not be finalised; lock it for the rest of the call
    select r.status into v_status from runs r where r.id = p_run_id for update;
    if not found then
        raise exception 'NOT_FOUND: run % does not exist', p_run_id
            using errcode = 'no_data_found';
    end if;
    if v_status = 'FINALISED' then
        raise exception 'RUN_STATE_CONFLICT: run % is FINALISED', p_run_id
            using errcode = 'object_not_in_prerequisite_state';
    end if;

    -- shape: four arrays of objects, each with a uuid id and (if given) this run's run_id.
    -- CASE keeps the regex test ahead of the ::uuid cast, so a bad string never raises 22P02.
    foreach v_key in array array['rows', 'groups', 'flags', 'norm_log'] loop
        if jsonb_typeof(p_payload -> v_key) is distinct from 'array' then
            raise exception 'CONTRACT_VIOLATION: payload.% must be an array', v_key
                using errcode = 'invalid_parameter_value';
        end if;
        select e.value::text into v_bad
        from jsonb_array_elements(p_payload -> v_key) as e
        where case
            when jsonb_typeof(e.value) <> 'object' then true
            when coalesce(e.value ->> 'id', '') !~* c_uuid then true
            when e.value ->> 'run_id' is null then false
            when e.value ->> 'run_id' !~* c_uuid then true
            else (e.value ->> 'run_id')::uuid <> p_run_id
        end
        limit 1;
        if v_bad is not null then
            raise exception 'CONTRACT_VIOLATION: payload.% element has a bad id or run_id: %',
                v_key, left(v_bad, 200)
                using errcode = 'invalid_parameter_value';
        end if;
    end loop;

    -- quantities travel as strings (constitution: never floating point)
    select e.value ->> 'source_row_id' into v_bad
    from jsonb_array_elements(p_payload -> 'rows') as e
    where jsonb_typeof(e.value -> 'qty') = 'number'
       or jsonb_typeof(e.value -> 'qty_base') = 'number'
    limit 1;
    if v_bad is not null then
        raise exception 'CONTRACT_VIOLATION: row % has a quantity as a JSON number, not a string',
            v_bad
            using errcode = 'invalid_parameter_value';
    end if;

    -- rows may only reference groups of this payload
    select e.value ->> 'source_row_id' into v_bad
    from jsonb_array_elements(p_payload -> 'rows') as e
    where case
        when e.value ->> 'group_id' is null then false
        when e.value ->> 'group_id' !~* c_uuid then true
        else not exists (
            select 1
            from jsonb_array_elements(p_payload -> 'groups') as g
            where (g.value ->> 'id')::uuid = (e.value ->> 'group_id')::uuid
        )
    end
    limit 1;
    if v_bad is not null then
        raise exception 'CONTRACT_VIOLATION: row % references a group that is not in the payload',
            v_bad
            using errcode = 'invalid_parameter_value';
    end if;

    -- duplicates inside the payload: report the unique constraint they would break (23505)
    -- instead of letting the upsert fail with 21000 ("cannot affect row a second time")
    select d.constraint_name, d.value into v_constraint, v_bad
    from (
        select k.constraint_name, e.value ->> k.field as value
        from (
            values
                ('rows', 'source_row_id', 'source_rows_run_row_uq', 1),
                ('rows', 'id', 'source_rows_pkey', 2),
                ('groups', 'group_key', 'groups_run_group_key_uq', 3),
                ('groups', 'id', 'groups_pkey', 4),
                ('flags', 'id', 'control_flags_pkey', 5),
                ('norm_log', 'id', 'normalisation_log_pkey', 6)
        ) as k (payload_key, field, constraint_name, rank)
        cross join lateral jsonb_array_elements(p_payload -> k.payload_key) as e
        group by k.rank, k.constraint_name, e.value ->> k.field
        having count(*) > 1
        order by k.rank
        limit 1
    ) as d;
    if v_constraint is not null then
        raise exception 'UNIQUE_VIOLATION: % appears more than once in the payload (%)',
            v_bad, v_constraint
            using errcode = 'unique_violation', constraint = v_constraint;
    end if;

    -- clear the previous reconcile outputs of this run
    delete from control_flags where run_id = p_run_id;
    delete from normalisation_log where run_id = p_run_id;
    update source_rows set group_id = null where run_id = p_run_id;
    delete from groups where run_id = p_run_id;

    -- groups first: source_rows (run_id, group_id) references them
    insert into groups (id, run_id, group_key, grn_no, row_ids, outcome, missing_from, diffs)
    select
        (g.value ->> 'id')::uuid,
        p_run_id,
        g.value ->> 'group_key',
        (g.value ->> 'grn_no')::integer,
        g.value -> 'row_ids',
        (g.value ->> 'outcome')::outcome,
        array(
            select m.value::source
            from jsonb_array_elements_text(g.value -> 'missing_from')
                with ordinality as m (value, ord)
            order by m.ord
        ),
        g.value -> 'diffs'
    from jsonb_array_elements(p_payload -> 'groups') as g;

    -- rows that left the payload go first, so their ids are free before the upsert
    delete from source_rows s
    where s.run_id = p_run_id
      and not exists (
          select 1
          from jsonb_array_elements(p_payload -> 'rows') as r
          where r.value ->> 'source_row_id' = s.source_row_id
      );

    -- rows: upsert on the natural key, so the stored run equals the payload
    insert into source_rows (
        id, run_id, source, source_row_id, row_kind, grn_no, date, supplier_code, item, lot_no,
        qty, uom, qty_base, base_uom, cell_status, raw, meta, duplicate_of, group_id, outcome
    )
    select
        (r.value ->> 'id')::uuid,
        p_run_id,
        (r.value ->> 'source')::source,
        r.value ->> 'source_row_id',
        (r.value ->> 'row_kind')::row_kind,
        (r.value ->> 'grn_no')::integer,
        (r.value ->> 'date')::date,
        r.value ->> 'supplier_code',
        r.value ->> 'item',
        r.value ->> 'lot_no',
        (r.value ->> 'qty')::numeric,
        r.value ->> 'uom',
        (r.value ->> 'qty_base')::numeric,
        r.value ->> 'base_uom',
        r.value -> 'cell_status',
        r.value -> 'raw',
        r.value -> 'meta',
        r.value ->> 'duplicate_of',
        (r.value ->> 'group_id')::uuid,
        (r.value ->> 'outcome')::outcome
    from jsonb_array_elements(p_payload -> 'rows') as r
    on conflict (run_id, source_row_id) do update set
        id            = excluded.id,
        source        = excluded.source,
        row_kind      = excluded.row_kind,
        grn_no        = excluded.grn_no,
        date          = excluded.date,
        supplier_code = excluded.supplier_code,
        item          = excluded.item,
        lot_no        = excluded.lot_no,
        qty           = excluded.qty,
        uom           = excluded.uom,
        qty_base      = excluded.qty_base,
        base_uom      = excluded.base_uom,
        cell_status   = excluded.cell_status,
        raw           = excluded.raw,
        meta          = excluded.meta,
        duplicate_of  = excluded.duplicate_of,
        group_id      = excluded.group_id,
        outcome       = excluded.outcome;

    -- flags and norm_log keep payload order (seq is assigned in this order)
    insert into control_flags (id, run_id, flag, grn_no, source_row_id, detail)
    select
        (f.elem ->> 'id')::uuid,
        p_run_id,
        (f.elem ->> 'flag')::flag_type,
        (f.elem ->> 'grn_no')::integer,
        f.elem ->> 'source_row_id',
        f.elem -> 'detail'
    from jsonb_array_elements(p_payload -> 'flags') with ordinality as f (elem, ord)
    order by f.ord;

    insert into normalisation_log (id, run_id, source_row_id, field, code, before, after)
    select
        (n.elem ->> 'id')::uuid,
        p_run_id,
        n.elem ->> 'source_row_id',
        n.elem ->> 'field',
        (n.elem ->> 'code')::norm_code,
        n.elem ->> 'before',
        n.elem ->> 'after'
    from jsonb_array_elements(p_payload -> 'norm_log') with ordinality as n (elem, ord)
    order by n.ord;
end;
$$;

-- only the API (service_role) may call it
revoke all on function replace_reconciliation(uuid, jsonb) from public, anon, authenticated;
grant execute on function replace_reconciliation(uuid, jsonb) to service_role;


-- ---------------------------------------------------------------------------------------------
-- 8. Storage buckets (private). No storage policies: only the service role reads and writes;
--    browsers use short-lived signed URLs issued by the API. Idempotent.
-- ---------------------------------------------------------------------------------------------
insert into storage.buckets (id, name, public)
values ('inputs', 'inputs', false), ('reports', 'reports', false)
on conflict (id) do nothing;
