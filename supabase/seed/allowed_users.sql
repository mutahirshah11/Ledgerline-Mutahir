-- Seed: demo allowlist users (US8 scenario 4, FR-034, data-model.md T-USERS, blueprint C13/R6).
--
-- All three addresses use the reserved `.example` domain (RFC 2606) and are not real mailboxes;
-- `.example` cannot receive mail, so `controller@kestrel.example` and `reviewer@kestrel.example`
-- sign in with email + password instead of a magic link (C13). This file is committed, so it
-- MUST NEVER contain a real email address.
--
-- The builder's own email is added separately, as CONTROLLER, by apps/api/scripts/seed_users.py
-- (never committed; comes from the BUILDER_EMAIL env var or a CLI argument).
--
-- apps/api/scripts/seed_users.py parses this exact `insert ... values (...)` statement so the
-- list of demo users lives in one place; keep each row on its own line as `('email', 'name', 'ROLE')`.
insert into allowed_users (email, display_name, role) values
    ('controller@kestrel.example', 'Controller (Demo)', 'CONTROLLER'),
    ('reviewer@kestrel.example', 'Reviewer (Demo)', 'REVIEWER'),
    ('ops@kestrel.example', 'Ops (Demo)', 'OPS')
on conflict (email) do nothing;
