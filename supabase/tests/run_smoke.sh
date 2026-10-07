#!/usr/bin/env bash
# Apply supabase_stub.sql -> migrations/0001_init.sql -> 0002_api_guards.sql -> 0003_review_guard.sql
# -> smoke.sql to an EMPTY Postgres database.
#
#   DATABASE_URL=postgres://postgres:pg@localhost:5432/postgres supabase/tests/run_smoke.sh
#
# Without a local psql, point PSQL at any psql command line instead, e.g. inside Docker:
#   PSQL="docker exec -i ll-pg psql -U postgres -d postgres" supabase/tests/run_smoke.sh
# (SQL is always sent on stdin, so the files need not exist where psql runs.)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
migration="$here/../migrations/0001_init.sql"
migration2="$here/../migrations/0002_api_guards.sql"
migration3="$here/../migrations/0003_review_guard.sql"

if [[ -n "${PSQL:-}" ]]; then
    read -r -a psql <<<"$PSQL"
else
    : "${DATABASE_URL:?set DATABASE_URL (or PSQL)}"
    psql=(psql "$DATABASE_URL")
fi

run() {
    local label="$1"
    shift
    echo "== $label"
    "${psql[@]}" -X -q -v ON_ERROR_STOP=1 "$@"
}

run "stub (roles, storage schema)" -f - <"$here/supabase_stub.sql"
run "migration 0001_init.sql (single transaction)" -1 -f - <"$migration"
run "migration 0002_api_guards.sql (single transaction)" -1 -f - <"$migration2"
run "migration 0003_review_guard.sql (single transaction)" -1 -f - <"$migration3"
run "smoke" \
    -v payload_a="$(cat "$here/fixtures/payload_a.json")" \
    -v payload_b="$(cat "$here/fixtures/payload_b.json")" \
    -f - <"$here/smoke.sql"
echo "== all done"
