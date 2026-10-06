"""The payload fixtures used by supabase/tests/smoke.sql are current and valid.

The CI `db` job feeds the committed JSON to Postgres without Python, so this test guards
against drift between the fixtures, `supabase/tests/make_payload.py` and the domain models.
"""

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from app.domain.models import ReconciliationPayload

SMOKE_DIR = Path(__file__).resolve().parents[3] / "supabase" / "tests"
NAMES = ["payload_a.json", "payload_b.json"]


def _make_payload() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_payload", SMOKE_DIR / "make_payload.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", NAMES)
def test_fixture_is_a_valid_payload(name: str) -> None:
    text = (SMOKE_DIR / "fixtures" / name).read_text(encoding="utf-8")
    payload = ReconciliationPayload.model_validate_json(text)
    # the JSON is exactly what the API would send for that payload
    assert json.loads(text) == payload.model_dump(mode="json")
    assert len({r.source_row_id for r in payload.rows}) == len(payload.rows)
    assert {r.run_id for r in payload.rows} == {payload.rows[0].run_id}


def test_fixtures_match_generator() -> None:
    generated = _make_payload().fixtures()
    assert sorted(generated) == NAMES
    for name, text in generated.items():
        assert (SMOKE_DIR / "fixtures" / name).read_text(encoding="utf-8") == text, (
            f"{name} is stale: run `uv run --project apps/api python "
            "supabase/tests/make_payload.py`"
        )


def test_payload_b_exercises_replace() -> None:
    a = ReconciliationPayload.model_validate_json(
        (SMOKE_DIR / "fixtures" / "payload_a.json").read_text(encoding="utf-8")
    )
    b = ReconciliationPayload.model_validate_json(
        (SMOKE_DIR / "fixtures" / "payload_b.json").read_text(encoding="utf-8")
    )
    a_rows = {r.source_row_id for r in a.rows}
    b_rows = {r.source_row_id for r in b.rows}
    assert a_rows - b_rows, "B must drop rows (prune)"
    assert b_rows - a_rows, "B must add rows (insert)"
    assert a_rows & b_rows, "B must keep rows (upsert)"
    assert [n.id for n in b.norm_log] != [
        n.id for n in a.norm_log if n.id in {m.id for m in b.norm_log}
    ], "B reorders norm_log"
