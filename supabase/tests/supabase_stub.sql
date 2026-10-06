-- Minimum of a Supabase database that 0001_init.sql relies on, for a plain Postgres 15/16
-- (local Docker or the CI service container). Not a migration; never applied to Supabase.
--
-- Mirrors Supabase where it matters for the smoke test:
--   * roles anon, authenticated (browser keys) and service_role (API key, bypasses RLS);
--   * default privileges that GRANT new public objects to all three roles, as Supabase does,
--     so the migration's REVOKEs are tested against a realistic starting point;
--   * storage.buckets with the columns the migration inserts.
-- Idempotent: roles are cluster-wide and survive a dropped database.

do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'anon') then
        create role anon nologin noinherit;
    end if;
    if not exists (select 1 from pg_roles where rolname = 'authenticated') then
        create role authenticated nologin noinherit;
    end if;
    if not exists (select 1 from pg_roles where rolname = 'service_role') then
        create role service_role nologin noinherit bypassrls;
    end if;
end;
$$;

grant usage on schema public to anon, authenticated, service_role;

alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
alter default privileges in schema public
    grant all on sequences to anon, authenticated, service_role;
alter default privileges in schema public
    grant all on functions to anon, authenticated, service_role;

create schema if not exists storage;
create table if not exists storage.buckets (
    id      text primary key,
    name    text not null,
    public  boolean default false
);
