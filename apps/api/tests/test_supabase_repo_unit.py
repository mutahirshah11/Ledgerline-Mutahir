"""SupabaseRepo mapping logic, offline (T064, research R12). A scripted stand-in for the
supabase client records each PostgREST call and answers from a queue; the full behaviour is
the shared contract suite under the `integration` marker (test_repo_contract_supabase.py)."""

from decimal import Decimal
from typing import Any

import httpx
import pytest
from postgrest.exceptions import APIError

from app.core.config import Settings
from app.core.errors import StateConflict, UniqueViolation, UpstreamFailed
from app.core.supabase_repo import SupabaseRepo
from app.domain.enums import RunStatus
from tests.factories import make_extraction, make_payload, make_row, make_run


class _Response:
    def __init__(self, data: Any) -> None:
        self.data = data


class FakeQuery:
    """Records every builder call; `execute()` answers with the next scripted result."""

    def __init__(self, client: "FakeClient", target: str) -> None:
        self.client = client
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        client.queries.append((target, self.calls))

    def __getattr__(self, name: str) -> Any:
        def record(*args: Any, **kwargs: Any) -> "FakeQuery":
            self.calls.append((name, args, kwargs))
            return self

        return record

    def execute(self) -> _Response:
        result = self.client.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return _Response(result)


class FakeClient:
    def __init__(self, *results: Any) -> None:
        self.results = list(results)
        self.queries: list[tuple[str, list[tuple[str, tuple[Any, ...], dict[str, Any]]]]] = []

    def table(self, name: str) -> FakeQuery:
        return FakeQuery(self, name)

    def rpc(self, name: str, params: dict[str, Any]) -> FakeQuery:
        query = FakeQuery(self, f"rpc:{name}")
        query.calls.append(("rpc", (params,), {}))
        return query

    def calls(self, i: int) -> list[tuple[str, tuple[Any, ...], dict[str, Any]]]:
        return self.queries[i][1]


def _repo(*results: Any) -> tuple[SupabaseRepo, FakeClient]:
    client = FakeClient(*results)
    settings = Settings(_env_file=None, SUPABASE_URL="https://test.supabase.co")
    return SupabaseRepo(settings, client=client, sleep=lambda _: None), client


def _unique(constraint: str) -> APIError:
    return APIError(
        {
            "code": "23505",
            "message": f'duplicate key value violates unique constraint "{constraint}"',
            "details": "Key (id)=(x) already exists.",
            "hint": None,
        }
    )


def _run_row(**over: Any) -> dict[str, Any]:
    return make_run(**over).model_dump(mode="json") | {"seq": 1}  # extra columns are dropped


def test_create_run_unique_violation_names_the_constraint() -> None:
    repo, _ = _repo(_unique("runs_pkey"))
    with pytest.raises(UniqueViolation) as exc:
        repo.create_run(make_run())
    assert exc.value.constraint == "runs_pkey"


def test_transition_zero_rows_is_state_conflict_with_current() -> None:
    run = make_run()
    moved = _run_row(id=run.id, status=RunStatus.UPLOADED)
    repo, client = _repo([_run_row(id=run.id)], [], [moved])  # read, CAS update, re-read
    with pytest.raises(StateConflict) as exc:
        repo.transition_run(run.id, RunStatus.UPLOADED, expected={RunStatus.CREATED})
    assert exc.value.current.status is RunStatus.UPLOADED
    update = client.calls(1)
    assert ("eq", ("status", "CREATED"), {}) in update  # compare-and-set on the status read
    assert update[0][0] == "update"


def test_source_rows_quantities_travel_as_strings() -> None:
    run = make_run()
    payload = make_payload(run.id)
    stored = payload.rows[0].model_dump(mode="json")
    repo, client = _repo(None, [stored])
    repo.replace_reconciliation(run.id, payload)
    [(_, (params,), _)] = client.calls(0)
    assert params["p_run_id"] == str(run.id)
    assert params["p_payload"]["rows"][0]["qty"] == "1.50"

    [row] = repo.list_source_rows(run.id)
    assert row.qty == Decimal("1.50")
    select = client.calls(1)[0]
    assert "qty::text" in select[1][0] and "qty_base::text" in select[1][0]


def test_numeric_from_postgrest_must_be_text() -> None:
    """A JSON number would arrive as a float; the select casts quantities to text."""
    run = make_run()
    row = make_row(run.id, "ERP-0001").model_dump(mode="json") | {"qty": "2.500"}
    repo, _ = _repo([row])
    assert repo.list_source_rows(run.id)[0].qty == Decimal("2.500")


def test_put_extraction_key_conflict_returns_existing() -> None:
    first = make_extraction(input_tokens=1)
    repo, _ = _repo(_unique("extractions_cache_uq"), [first.model_dump(mode="json")])
    stored, created = repo.put_extraction(make_extraction(input_tokens=2))
    assert created is False
    assert stored == first


def test_run_events_newest_first_then_seq() -> None:
    run = make_run()
    repo, client = _repo([])
    assert repo.list_run_events(run.id) == []
    orders = [c for c in client.calls(0) if c[0] == "order"]
    assert orders == [("order", ("at",), {"desc": True}), ("order", ("seq",), {"desc": True})]


def test_transient_errors_retried_then_upstream_failed() -> None:
    down = APIError({"code": "PGRST001", "message": "db down", "details": None, "hint": None})
    repo, client = _repo(httpx.ConnectError("refused"), down, down)
    with pytest.raises(UpstreamFailed) as exc:
        repo.get_run(make_run().id)
    assert exc.value.service == "supabase"
    assert exc.value.attempts == 3
    assert len(client.queries) == 3


def test_replace_reconciliation_maps_sql_errors() -> None:
    run = make_run()
    payload = make_payload(run.id)
    bad = APIError({"code": "22023", "message": "CONTRACT_VIOLATION: x", "details": None})
    dup = APIError({"code": "23505", "message": "UNIQUE_VIOLATION: k appears twice (groups_pkey)"})
    final = _run_row(id=run.id, status=RunStatus.FINALISED, run_key="k")
    final |= {"finalised_at": final["created_at"], "finalised_by": "c@example.com"}
    conflict = APIError({"code": "55000", "message": "RUN_STATE_CONFLICT: run is FINALISED"})
    repo, _ = _repo(bad, dup, conflict, [final])
    with pytest.raises(ValueError):
        repo.replace_reconciliation(run.id, payload)
    with pytest.raises(UniqueViolation) as exc:
        repo.replace_reconciliation(run.id, payload)
    assert exc.value.constraint == "groups_pkey"
    with pytest.raises(StateConflict) as conflict_exc:
        repo.replace_reconciliation(run.id, payload)
    assert conflict_exc.value.current.status is RunStatus.FINALISED


def test_ping_false_when_not_configured() -> None:
    """No URL or key: ping reports False instead of raising (EP-01 shows it as down)."""
    assert SupabaseRepo(Settings(_env_file=None)).ping() is False
