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
from tests.factories import (
    at,
    make_audit,
    make_extraction,
    make_flag,
    make_notification,
    make_page,
    make_payload,
    make_proof,
    make_review,
    make_row,
    make_run,
)


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
    repo, client = _repo(None, [stored], [])
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
    repo, _ = _repo([row], [])
    assert repo.list_source_rows(run.id)[0].qty == Decimal("2.500")


def test_put_extraction_key_conflict_returns_existing() -> None:
    first = make_extraction(input_tokens=1)
    repo, _ = _repo(_unique("extractions_cache_uq"), [first.model_dump(mode="json")])
    stored, created = repo.put_extraction(make_extraction(input_tokens=2))
    assert created is False
    assert stored == first


def test_run_events_newest_first_then_seq() -> None:
    """repo-interface.md: `at` desc, then newest insertion first (C27 seq)."""
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


# --- review fixes: retries after a write that already landed, paging, guards --------------

CONNECT = httpx.ConnectError("refused")


def _conflict(message: str = "RUN_STATE_CONFLICT: run is FINALISED") -> APIError:
    return APIError({"code": "55000", "message": message})


def test_retried_insert_that_landed_returns_the_stored_row() -> None:
    run = make_run()
    repo, _ = _repo(CONNECT, _unique("runs_pkey"), [_run_row(id=run.id)])
    assert repo.create_run(run) == run


def test_retried_insert_of_another_row_is_still_a_violation() -> None:
    run = make_run()
    other = _run_row(id=run.id, month="2026-08")
    repo, _ = _repo(CONNECT, _unique("runs_pkey"), [other])
    with pytest.raises(UniqueViolation):
        repo.create_run(run)


def test_existing_returned_methods_count_our_retried_insert_as_created() -> None:
    run = make_run()
    n = make_notification(run.id)
    page = make_page(run.id)
    repo, _ = _repo(
        CONNECT,
        _unique("notifications_idempotency_key_uq"),
        [n.model_dump(mode="json")],
        CONNECT,
        _unique("page_manifest_pkey"),
        [page.model_dump(mode="json")],
        _unique("page_manifest_pkey"),
        [page.model_dump(mode="json")],
        CONNECT,
        _unique("page_manifest_pkey"),
        [make_page(run.id, page_no=2).model_dump(mode="json")],
    )
    assert repo.insert_notification(n) == (n, True)
    assert repo.add_page_if_new(page) == (page, True)
    assert repo.add_page_if_new(page) == (page, False)  # one attempt: someone else's
    # same run, but not the row we sent: not ours (the whole row is compared)
    assert repo.add_page_if_new(page)[1] is False


def test_retried_cas_that_landed_returns_the_row() -> None:
    run = make_run()
    moved = _run_row(id=run.id, status=RunStatus.UPLOADED)
    repo, _ = _repo([_run_row(id=run.id)], CONNECT, [], [moved])
    got = repo.transition_run(run.id, RunStatus.UPLOADED, expected={RunStatus.CREATED})
    assert (got.id, got.status) == (run.id, RunStatus.UPLOADED)


def test_retried_cas_that_did_not_land_is_a_conflict() -> None:
    run = make_run()
    failed = _run_row(id=run.id, status=RunStatus.FAILED, failed_stage="upload")
    repo, _ = _repo([_run_row(id=run.id)], CONNECT, [], [failed])
    with pytest.raises(StateConflict):
        repo.transition_run(run.id, RunStatus.UPLOADED, expected={RunStatus.CREATED})


def test_append_audit_retries_only_before_the_server() -> None:
    entry = make_audit()
    stored = make_audit(id=7).model_dump(mode="json")
    repo, client = _repo(CONNECT, [stored])
    assert repo.append_audit(entry).id == 7
    assert len(client.queries) == 2
    repo, client = _repo(httpx.ReadTimeout("slow"), [stored])
    with pytest.raises(UpstreamFailed) as exc:
        repo.append_audit(entry)
    assert exc.value.attempts == 1
    assert len(client.queries) == 1


def test_lists_page_past_the_postgrest_row_cap() -> None:
    run = make_run()
    flags = [make_flag(run.id).model_dump(mode="json") for _ in range(1001)]
    repo, client = _repo(flags[:1000], flags[1000:], [])
    assert len(repo.list_flags(run.id)) == 1001
    ranges = [c for i in range(3) for c in client.calls(i) if c[0] == "range"]
    assert ranges == [
        ("range", (0, 999), {}),
        ("range", (1000, 1999), {}),
        ("range", (1001, 2000), {}),
    ]
    orders = [c[1] for c in client.calls(0) if c[0] == "order"]
    assert orders == [("seq",)]  # a total order, so pages never overlap


def test_short_page_is_not_the_end() -> None:
    """A project with a lower max-rows returns short pages; only an empty page ends the list."""
    run = make_run()
    flags = [make_flag(run.id).model_dump(mode="json") for _ in range(3)]
    repo, client = _repo(flags[:2], flags[2:], [])
    assert len(repo.list_flags(run.id)) == 3
    assert len(client.queries) == 3


def test_upsert_review_items_chunks_and_checks_every_key() -> None:
    run = make_run()
    items = [make_review(run.id, f"CELL:REG-p1-r01:k{i}") for i in range(501)]
    rows = [i.model_dump(mode="json") for i in items]
    repo, client = _repo([_run_row(id=run.id)], rows[:500], rows[500:])
    assert repo.upsert_review_items(run.id, items) == items
    sizes = [len(client.calls(i)[0][1][0]["p_items"]) for i in (1, 2)]
    assert sizes == [500, 1]
    repo, _ = _repo([_run_row(id=run.id)], rows[:1])
    with pytest.raises(UpstreamFailed):
        repo.upsert_review_items(run.id, items[:2])


def test_upsert_review_items_is_one_rpc() -> None:
    run = make_run()
    item = make_review(run.id, "CELL:REG-p1-r01:qty")
    repo, client = _repo([_run_row(id=run.id)], [item.model_dump(mode="json")])
    assert repo.upsert_review_items(run.id, [item]) == [item]
    [(name, (params,), _)] = client.calls(1)
    assert client.queries[1][0] == "rpc:upsert_review_items"
    assert params == {"p_run_id": str(run.id), "p_items": [item.model_dump(mode="json")]}


def test_save_proof_and_delete_file_map_trigger_conflict() -> None:
    run = make_run()
    final = _run_row(id=run.id, status=RunStatus.UPLOADED)
    proof = make_proof(run.id)
    repo, _ = _repo(_conflict(), [final])
    with pytest.raises(StateConflict) as exc:
        repo.save_proof(proof)
    assert exc.value.current.status is RunStatus.UPLOADED

    from tests.factories import make_file

    file = make_file(run.id)
    repo, _ = _repo([file.model_dump(mode="json")], _conflict("FILE_LOCKED"), [final])
    with pytest.raises(StateConflict) as exc:
        repo.delete_source_file(file.id)
    assert exc.value.current.status is RunStatus.UPLOADED


def test_add_source_file_maps_trigger_conflict() -> None:
    """0002 `source_files_insert_only_created` (55000) → StateConflict(run)."""
    from tests.factories import make_file

    run = make_run()
    uploaded = _run_row(id=run.id, status=RunStatus.UPLOADED)
    repo, _ = _repo(_conflict("RUN_STATE_CONFLICT: run is not CREATED"), [uploaded])
    with pytest.raises(StateConflict) as exc:
        repo.add_source_file(make_file(run.id))
    assert exc.value.current.status is RunStatus.UPLOADED


def test_confirm_source_file_rejects_naive_datetime() -> None:
    from datetime import datetime

    from tests.factories import make_file

    run = make_run()
    file = make_file(run.id)
    repo, client = _repo([file.model_dump(mode="json")])
    with pytest.raises(ValueError):
        repo.confirm_source_file(file.id, datetime(2026, 10, 1, 9, 0))  # noqa: DTZ001
    assert len(client.queries) == 1  # no update sent
    assert at(1).tzinfo is not None
