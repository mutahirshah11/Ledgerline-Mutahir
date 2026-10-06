"""Static checks of supabase/migrations/0001_init.sql (T027).

Sources: contracts/migration-0001.md (items 1 to 8, Verification), data-model.md Section 5,
blueprint Section 10 and change log C7 to C9, constitution (numeric not float, scenario clock,
RLS with no policies, private buckets, append-only audit).
"""

import re

import pytest

from app.domain.enums import Stage
from tests.sql_helpers import (
    function_body,
    raw_sql,
    sql,
    table_bodies,
    table_columns,
    table_constraints,
)

# Tables in FK order (contract item 3).
TABLES = [
    "allowed_users",
    "runs",
    "source_files",
    "page_manifest",
    "extractions",
    "groups",
    "source_rows",
    "normalisation_log",
    "control_flags",
    "review_items",
    "proofs",
    "notifications",
    "run_events",
    "audit_log",
]

# data-model.md Section 5: (column, type, nullable). "null" in the doc means nullable;
# every other column is NOT NULL.
COLUMNS: dict[str, list[tuple[str, str, bool]]] = {
    "allowed_users": [
        ("email", "text", False),
        ("display_name", "text", False),
        ("role", "role", False),
    ],
    "runs": [
        ("id", "uuid", False),
        ("month", "text", False),
        ("status", "run_status", False),
        ("run_key", "text", True),
        ("failed_stage", "text", True),
        ("error_message", "text", True),
        ("scenario_now", "timestamptz", False),
        ("created_by", "text", False),
        ("created_at", "timestamptz", False),
        ("finalised_at", "timestamptz", True),
        ("finalised_by", "text", True),
    ],
    "source_files": [
        ("id", "uuid", False),
        ("run_id", "uuid", False),
        ("source", "source", False),
        ("filename", "text", False),
        ("storage_path", "text", False),
        ("sha256", "text", False),
        ("size_bytes", "bigint", False),
        ("page_no", "integer", True),
        ("confirmed_at", "timestamptz", True),
    ],
    "page_manifest": [
        ("sha256", "text", False),
        ("month", "text", False),
        ("page_no", "integer", False),
        ("first_seen_run_id", "uuid", False),
        ("first_seen_at", "timestamptz", False),
    ],
    "extractions": [
        ("id", "uuid", False),
        ("page_sha256", "text", False),
        ("read_variant", "read_variant", False),
        ("model", "text", False),
        ("prompt_version", "text", False),
        ("result", "jsonb", False),
        ("input_tokens", "integer", False),
        ("output_tokens", "integer", False),
        ("latency_ms", "integer", False),
        ("created_at", "timestamptz", False),
    ],
    "groups": [
        ("id", "uuid", False),
        ("run_id", "uuid", False),
        ("group_key", "text", False),
        ("grn_no", "integer", True),
        ("row_ids", "jsonb", False),
        ("outcome", "outcome", False),
        ("missing_from", "source[]", False),
        ("diffs", "jsonb", False),
    ],
    "source_rows": [
        ("id", "uuid", False),
        ("run_id", "uuid", False),
        ("source", "source", False),
        ("source_row_id", "text", False),
        ("row_kind", "row_kind", False),
        ("grn_no", "integer", True),
        ("date", "date", True),
        ("supplier_code", "text", True),
        ("item", "text", True),
        ("lot_no", "text", True),
        ("qty", "numeric", True),
        ("uom", "text", True),
        ("qty_base", "numeric", True),
        ("base_uom", "text", True),
        ("cell_status", "jsonb", False),
        ("raw", "jsonb", False),
        ("meta", "jsonb", False),
        ("duplicate_of", "text", True),
        ("group_id", "uuid", True),
        ("outcome", "outcome", True),
    ],
    "normalisation_log": [
        ("id", "uuid", False),
        ("run_id", "uuid", False),
        ("source_row_id", "text", False),
        ("field", "text", False),
        ("code", "norm_code", False),
        ("before", "text", True),
        ("after", "text", True),
    ],
    "control_flags": [
        ("id", "uuid", False),
        ("run_id", "uuid", False),
        ("flag", "flag_type", False),
        ("grn_no", "integer", True),
        ("source_row_id", "text", True),
        ("detail", "jsonb", False),
    ],
    "review_items": [
        ("id", "uuid", False),
        ("run_id", "uuid", False),
        ("kind", "review_kind", False),
        ("item_key", "text", False),
        ("grn_no", "integer", True),
        ("source_row_id", "text", True),
        ("field", "text", True),
        ("reason", "text", False),
        ("read_a", "jsonb", True),
        ("read_b", "jsonb", True),
        ("context", "jsonb", False),
        ("status", "review_status", False),
        ("resolved_value", "text", True),
        ("resolved_by", "text", True),
        ("resolved_at", "timestamptz", True),
        ("note", "text", True),
    ],
    "proofs": [
        ("run_id", "uuid", False),
        ("status", "proof_status", False),
        ("checks", "jsonb", False),
        ("computed_at", "timestamptz", False),
    ],
    "notifications": [
        ("id", "uuid", False),
        ("run_id", "uuid", False),
        ("kind", "notification_kind", False),
        ("idempotency_key", "text", False),
        ("status", "notification_status", False),
        ("attempts", "integer", False),
        ("last_error", "text", True),
        ("telegram_message_id", "bigint", True),
        ("sent_at", "timestamptz", True),
        ("updated_at", "timestamptz", False),
    ],
    "run_events": [
        ("id", "uuid", False),
        ("run_id", "uuid", False),
        ("stage", "text", False),
        ("status", "run_event_status", False),
        ("at", "timestamptz", False),
        ("duration_ms", "integer", True),
        ("detail", "jsonb", False),
    ],
    "audit_log": [
        ("id", "bigserial", False),
        ("at", "timestamptz", False),
        ("actor", "text", False),
        ("action", "text", False),
        ("entity", "text", False),
        ("entity_id", "text", False),
        ("run_id", "uuid", True),
        ("before", "jsonb", True),
        ("after", "jsonb", True),
        ("channel", "audit_channel", False),
    ],
}

PRIMARY_KEYS = {t: ("id",) for t in TABLES} | {
    "allowed_users": ("email",),
    "page_manifest": ("sha256",),
    "proofs": ("run_id",),
}

# data-model.md Section 5 uniques (besides primary keys and the C7 partial index).
UNIQUES: dict[str, tuple[str, ...]] = {
    "source_files": ("run_id", "sha256"),
    "extractions": ("page_sha256", "read_variant", "model", "prompt_version"),
    "source_rows": ("run_id", "source_row_id"),
    "groups": ("run_id", "group_key"),
    "review_items": ("run_id", "item_key"),
    "notifications": ("idempotency_key",),
}

# (table, column, referenced table)
FOREIGN_KEYS = [
    ("source_files", "run_id", "runs"),
    ("page_manifest", "first_seen_run_id", "runs"),
    ("groups", "run_id", "runs"),
    ("source_rows", "run_id", "runs"),
    ("source_rows", "group_id", "groups"),
    ("normalisation_log", "run_id", "runs"),
    ("control_flags", "run_id", "runs"),
    ("review_items", "run_id", "runs"),
    ("proofs", "run_id", "runs"),
    ("notifications", "run_id", "runs"),
    ("run_events", "run_id", "runs"),
]

TYPE_ALIASES = {
    "int": "integer",
    "int4": "integer",
    "int8": "bigint",
    "serial8": "bigserial",
    "timestamp with time zone": "timestamptz",
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _col_type(definition: str) -> str:
    if definition.startswith("timestamp with time zone"):
        return "timestamptz"
    first = definition.split(" ", 1)[0]
    return TYPE_ALIASES.get(first, first)


def _has_unique(table: str, cols: tuple[str, ...]) -> bool:
    body = table_bodies()[table]
    target = "(" + ",".join(cols) + ")"
    for c in table_constraints(body):
        if re.search(r"\bunique\b", c) and _norm(c).endswith("unique" + target):
            return True
    if len(cols) == 1 and re.search(r"\bunique\b", table_columns(body).get(cols[0], "")):
        return True
    pattern = rf"create unique index (?:if not exists )?\w+ on (?:public\.)?{table} ?\(([^)]*)\)"
    return any(
        _norm(m.group(1)) == ",".join(cols) and not sql()[m.end() :].lstrip().startswith("where")
        for m in re.finditer(pattern, sql())
    )


# --- tables and columns --------------------------------------------------------------------


def test_fourteen_tables() -> None:
    assert list(table_bodies()) == TABLES  # exactly these, in FK order


@pytest.mark.parametrize("table", TABLES)
def test_columns_match_data_model(table: str) -> None:
    cols = table_columns(table_bodies()[table])
    assert list(cols) == [c for c, _, _ in COLUMNS[table]]
    pk = PRIMARY_KEYS[table]
    for name, typ, nullable in COLUMNS[table]:
        definition = cols[name]
        assert _col_type(definition) == typ, f"{table}.{name}: {definition}"
        is_not_null = "not null" in definition or (pk == (name,) and "primary key" in definition)
        assert is_not_null != nullable, f"{table}.{name} nullability: {definition}"


@pytest.mark.parametrize("table", TABLES)
def test_primary_keys(table: str) -> None:
    body = table_bodies()[table]
    pk = PRIMARY_KEYS[table]
    inline = len(pk) == 1 and "primary key" in table_columns(body)[pk[0]]
    clause = any(
        _norm(c).endswith("primarykey(" + ",".join(pk) + ")") for c in table_constraints(body)
    )
    assert inline or clause


@pytest.mark.parametrize(("table", "column", "ref"), FOREIGN_KEYS)
def test_foreign_keys_restrict(table: str, column: str, ref: str) -> None:
    body = table_bodies()[table]
    defs = [table_columns(body)[column]] + [
        c for c in table_constraints(body) if f"foreign key ({column})" in c
    ]
    fk = [d for d in defs if re.search(rf"references (?:public\.)?{ref} ?\(", d)]
    assert len(fk) == 1, f"{table}.{column} → {ref}"
    assert "on delete restrict" in fk[0]


def test_group_fk_deferrable() -> None:
    body = table_bodies()["source_rows"]
    defs = [table_columns(body)["group_id"]] + [
        c for c in table_constraints(body) if "foreign key (group_id)" in c
    ]
    assert any("deferrable initially deferred" in d for d in defs)


def test_audit_log_run_id_indexed() -> None:
    assert re.search(
        r"create index (?:if not exists )?\w+ on (?:public\.)?audit_log ?\(run_id\)", sql()
    )


def test_source_rows_lookup_index() -> None:
    assert re.search(
        r"create index (?:if not exists )?\w+ on (?:public\.)?source_rows "
        r"?\(run_id, ?source, ?source_row_id\)",
        sql(),
    )


# --- constraints ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", sorted(UNIQUES))
def test_required_uniques_present(table: str) -> None:
    assert _has_unique(table, UNIQUES[table]), f"unique {UNIQUES[table]} on {table}"


def test_run_key_partial_unique_on_finalised() -> None:
    assert re.search(
        r"create unique index (?:if not exists )?runs_run_key_finalised_uq "
        r"on (?:public\.)?runs ?\(run_key\) where \(?status ?= ?'finalised'(?:::run_status)?\)?",
        sql(),
    )
    # and run_key is not unique on its own (re-runs of unfinished inputs allowed, C7)
    assert "unique" not in table_columns(table_bodies()["runs"])["run_key"]
    assert not _has_unique("runs", ("run_key",))


def _checks(table: str) -> list[str]:
    body = table_bodies()[table]
    return [c for c in table_constraints(body) if re.search(r"\bcheck\b", c)] + [
        d for d in table_columns(body).values() if " check " in f" {d}"
    ]


def test_required_checks_present() -> None:
    checks = {t: [_norm(c) for c in _checks(t)] for t in TABLES}
    assert any("email=lower(email)" in c for c in checks["allowed_users"])
    assert any(r"month~'^\d{4}-(0[1-9]|1[0-2])$'" in c for c in checks["runs"])
    assert any("page_noisnullorsource='register'" in c for c in checks["source_files"])
    assert any("status<>'cannot_determine'ornoteisnotnull" in c for c in checks["review_items"])


@pytest.mark.parametrize(("table", "column"), [("runs", "failed_stage"), ("run_events", "stage")])
def test_stage_check_lists_every_stage(table: str, column: str) -> None:
    stage_checks = [c for c in _checks(table) if column in c]
    assert len(stage_checks) == 1
    listed = re.findall(r"'([a-z]+)'", stage_checks[0])
    assert listed == [s.value for s in Stage]


def test_no_float_columns() -> None:
    assert not re.search(r"\b(real|double precision|float[48]?)\b", sql())


def test_quantities_are_numeric() -> None:
    cols = table_columns(table_bodies()["source_rows"])
    assert _col_type(cols["qty"]) == "numeric"
    assert _col_type(cols["qty_base"]) == "numeric"


def test_no_timestamp_defaults() -> None:
    text = sql()
    assert not re.search(
        r"default (now\(\)|current_timestamp|current_date|localtimestamp|"
        r"clock_timestamp|statement_timestamp|transaction_timestamp|'now')",
        text,
    )
    for table in TABLES:
        for name, definition in table_columns(table_bodies()[table]).items():
            if _col_type(definition) in {"timestamptz", "date"}:
                assert "default" not in definition, f"{table}.{name}"


# --- security --------------------------------------------------------------------------------


def test_rls_enabled_on_all_tables() -> None:
    enabled = re.findall(r"alter table (?:public\.)?(\w+) enable row level security", sql())
    assert sorted(enabled) == sorted(TABLES)


def test_no_policies() -> None:
    assert "create policy" not in sql()


def test_tables_revoked_from_anon_and_authenticated() -> None:
    text = sql()
    every_table = re.search(
        r"revoke all on all tables in schema public from (anon, ?authenticated|"
        r"authenticated, ?anon)",
        text,
    )
    per_table = all(
        re.search(rf"revoke all on (?:table )?(?:public\.)?{t} from (anon|authenticated)", text)
        for t in TABLES
    )
    assert every_table or per_table


def test_audit_trigger_blocks_update_delete_truncate() -> None:
    fn = function_body("audit_log_append_only")
    assert "returns trigger" in fn
    assert re.search(r"raise exception 'audit_log is append-only'", fn)
    text = sql()
    row = re.search(
        r"create trigger \w+ before (update or delete|delete or update) on (?:public\.)?audit_log "
        r"for each row execute (?:function|procedure) (?:public\.)?audit_log_append_only\(\)",
        text,
    )
    stmt = re.search(
        r"create trigger \w+ before truncate on (?:public\.)?audit_log "
        r"for each statement execute (?:function|procedure) (?:public\.)?audit_log_append_only\(\)",
        text,
    )
    assert row and stmt


def test_replace_reconciliation_defined() -> None:
    fn = function_body("replace_reconciliation")
    assert re.search(r"replace_reconciliation ?\(p_run_id uuid, ?p_payload jsonb\)", fn)
    assert "returns void" in fn
    assert "language plpgsql" in fn
    assert "security invoker" in fn
    assert "security definer" not in fn
    # refuses a finalised run
    assert "'finalised'" in fn and "run_state_conflict" in fn
    # rebuilds reconcile outputs
    for stmt in (
        r"delete from (?:public\.)?control_flags where run_id = p_run_id",
        r"delete from (?:public\.)?normalisation_log where run_id = p_run_id",
        r"update (?:public\.)?source_rows set group_id = null where run_id = p_run_id",
        r"delete from (?:public\.)?groups where run_id = p_run_id",
    ):
        assert re.search(stmt, fn), stmt
    for key in ("groups", "rows", "flags", "norm_log"):
        assert f"p_payload->'{key}'" in fn, key
    assert re.search(r"on conflict ?\(run_id, ?source_row_id\) do update", fn)
    assert "::numeric" in fn
    # never touches tables outside the rebuilt set
    for table in ("review_items", "proofs", "audit_log", "notifications", "run_events"):
        assert table not in fn, table


def test_replace_reconciliation_step_order() -> None:
    fn = function_body("replace_reconciliation")
    # contract item 7 order: clear outputs; insert groups (FK target) before upserting rows;
    # prune rows not in the payload; then flags and norm_log
    steps = [
        r"delete from (?:public\.)?control_flags where",
        r"delete from (?:public\.)?normalisation_log where",
        r"update (?:public\.)?source_rows set group_id = null",
        r"delete from (?:public\.)?groups where",
        r"insert into (?:public\.)?groups ?\(",
        r"insert into (?:public\.)?source_rows ?\(",
        r"delete from (?:public\.)?source_rows ",
        r"insert into (?:public\.)?control_flags ?\(",
        r"insert into (?:public\.)?normalisation_log ?\(",
    ]
    found = [re.search(s, fn) for s in steps]
    assert all(found), [s for s, m in zip(steps, found, strict=True) if m is None]
    order = [m.start() for m in found if m is not None]
    assert order == sorted(order)


def test_replace_reconciliation_execute_only_service_role() -> None:
    text = sql()
    sig = r"function (?:public\.)?replace_reconciliation ?\(uuid, ?jsonb\)"
    revoke = re.search(rf"revoke all on {sig} from ([\w, ]+);", text)
    assert revoke is not None
    revoked = {r.strip() for r in revoke.group(1).split(",")}
    assert {"public", "anon", "authenticated"} <= revoked
    assert re.search(rf"grant execute on {sig} to service_role;", text)
    assert not re.search(rf"grant [\w ,]+ on {sig} to [\w, ]*(anon|authenticated|public)", text)


# --- storage -------------------------------------------------------------------------------


def test_buckets_private() -> None:
    m = re.search(
        r"insert into storage\.buckets ?\(id, ?name, ?public\) values (.*?) "
        r"on conflict (?:\(id\) )?do nothing",
        sql(),
    )
    assert m is not None
    rows = re.findall(r"\('(\w+)', ?'(\w+)', ?(\w+)\)", m.group(1))
    assert sorted(rows) == [("inputs", "inputs", "false"), ("reports", "reports", "false")]
    assert "public = true" not in sql() and ", true)" not in sql()


def test_pgcrypto_extension() -> None:
    assert re.search(r"create extension if not exists \"?pgcrypto\"?", sql())


def test_migration_is_commented_and_sectioned() -> None:
    # readability: one numbered header comment per contract item, in contract order
    headers = re.findall(r"^-- (\d)\. (\w+)", raw_sql(), flags=re.MULTILINE)
    assert [int(n) for n, _ in headers] == list(range(1, 9))
    assert [w.lower() for _, w in headers] == [
        "extensions",
        "enum",
        "tables",
        "indexes",
        "row",
        "audit",
        "replace_reconciliation",
        "storage",
    ]


def test_parser_sees_every_create_table() -> None:
    # guard against the parser silently dropping a table
    assert sql().count("create table") == len(table_bodies()) == 14
