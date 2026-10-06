"""Pydantic models match the SQL tables (data-model.md Section 5): same columns, same nullability.

Columns are parsed from `supabase/migrations/0001_init.sql`. jsonb columns hold nested models
(e.g. `result: PageRead`, `meta: RowMeta`); only the column name and nullability are compared.
"""

import types
from typing import Any, Union, get_args, get_origin

import pytest
from pydantic import BaseModel

from app.domain import models as m
from tests.sql_helpers import table_bodies, table_columns

TABLE_MODELS: dict[str, type[BaseModel]] = {
    "allowed_users": m.AllowedUser,
    "runs": m.Run,
    "source_files": m.SourceFile,
    "page_manifest": m.PageManifestEntry,
    "extractions": m.Extraction,
    "source_rows": m.CanonicalRow,
    "groups": m.Group,
    "normalisation_log": m.NormLogEntry,
    "control_flags": m.ControlFlag,
    "review_items": m.ReviewItem,
    "proofs": m.Proof,
    "notifications": m.Notification,
    "run_events": m.RunEvent,
    "audit_log": m.AuditEntry,
}

# Documented differences. `seq` is a DB-only insertion-order column (identity), never in a model.
DB_ONLY_COLUMNS = {"seq"}


def _sql_nullable(definition: str) -> bool:
    return "not null" not in definition and "primary key" not in definition


def _model_nullable(annotation: Any) -> bool:
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        return type(None) in get_args(annotation)
    return annotation is type(None)


def test_every_table_is_mapped() -> None:
    assert set(table_bodies()) == set(TABLE_MODELS)


@pytest.mark.parametrize("table", sorted(TABLE_MODELS))
def test_model_fields_equal_sql_columns(table: str) -> None:
    columns = {
        name: definition
        for name, definition in table_columns(table_bodies()[table]).items()
        if name not in DB_ONLY_COLUMNS
    }
    fields = TABLE_MODELS[table].model_fields
    assert set(fields) == set(columns)
    nullability = {name: _model_nullable(f.annotation) for name, f in fields.items()}
    assert nullability == {name: _sql_nullable(d) for name, d in columns.items()}
