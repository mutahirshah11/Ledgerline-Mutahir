"""F-08 final-review hardening (005 final review I-6, I-7, M-3, M-7): a decision cannot land on
a FINALISED run (repo guard, migration 0003), the open-item rule reads the audit once, and
EP-14 / EP-15 keep their repo calls bounded (NFR-05)."""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.db import Repo
from app.core.errors import StateConflict
from app.domain.enums import AuditAction, AuditChannel, ReviewStatus, RunStatus
from app.domain.models import Run
from app.pipeline import engine
from tests.factories import at, make_audit, make_review, make_run
from tests.support.pack_run import CONTROLLER, PackRun
from tests.test_repo_contract import _finalise, _new_run
from tests.test_supabase_repo_unit import _conflict, _repo, _run_row

API = "/api/v1"
Client = Callable[[str], TestClient]
QTY_4419 = "CELL:REG-p2-r05:qty"
ACCEPT = {"action": "ACCEPT_READ"}


def _decide(repo: Repo, item_id: Any) -> Any:
    return repo.resolve_review_item(
        item_id,
        status=ReviewStatus.RESOLVED,
        resolved_value="12",
        resolved_by="reviewer@example.com",
        resolved_at=at(10),
        note=None,
    )


# --- I-6: no decision on a FINALISED run (repo contract, migration 0003) ------------------


def test_F08_R8_repo_refuses_decision_on_finalised_run(repo: Repo) -> None:
    run = _new_run(repo)
    [item, done] = repo.upsert_review_items(
        run.id,
        [make_review(run.id, "CELL:REG-p1-r01:qty"), make_review(run.id, "CELL:REG-p1-r02:qty")],
    )
    decided = _decide(repo, done.id)
    final = _finalise(repo, run, "k-final")
    with pytest.raises(StateConflict) as exc:
        _decide(repo, item.id)
    assert exc.value.current == final  # the run, as the 0003 trigger reports it
    assert repo.get_review_item(item.id).status is ReviewStatus.OPEN
    with pytest.raises(StateConflict) as second:  # an item already decided: the item wins
        _decide(repo, done.id)
    assert second.value.current == decided


def test_F08_R8_repo_refuses_review_refresh_on_finalised_run(repo: Repo) -> None:
    """The 0003 trigger also refuses the upsert's refresh of an existing item (an UPDATE)."""
    run = _new_run(repo)
    [item] = repo.upsert_review_items(run.id, [make_review(run.id, "CELL:REG-p1-r01:qty")])
    final = _finalise(repo, run, "k-final")
    with pytest.raises(StateConflict) as exc:
        repo.upsert_review_items(run.id, [item.model_copy(update={"reason": item.reason})])
    assert exc.value.current == final
    assert repo.list_review_items(run.id) == [item]


def test_F08_R8_supabase_maps_review_guard_to_run_conflict() -> None:
    """0003 `review_items_refuse_finalised_run` (55000) → StateConflict(run)."""
    run = make_run()
    item = make_review(run.id, "CELL:REG-p1-r01:qty")
    final = _run_row(id=run.id, status=RunStatus.FINALISED, run_key="k")
    final |= {"finalised_at": final["created_at"], "finalised_by": "c@example.com"}
    repo, _ = _repo([item.model_dump(mode="json")], _conflict(), [final])
    with pytest.raises(StateConflict) as exc:
        _decide(repo, item.id)
    assert isinstance(exc.value.current, Run)
    assert exc.value.current.status is RunStatus.FINALISED


def test_F08_R8_finalise_racing_decision_is_409_without_audit(
    pack_run: PackRun, api_client_as: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run is finalised after EP-15's status guard but before its write: the repo refuses
    (StateConflict(run)) and EP-15 answers 409 RUN_STATE_CONFLICT with no audit entry."""
    repo = pack_run.repo
    real = repo.resolve_review_item

    def finalise_first(*args: Any, **kwargs: Any) -> Any:
        repo.transition_run(
            pack_run.run_id,
            RunStatus.FINALISED,
            expected={RunStatus.AWAITING_REVIEW},
            finalised_by=CONTROLLER.email,
            finalised_at=pack_run.run.scenario_now,
        )
        return real(*args, **kwargs)

    monkeypatch.setattr(repo, "resolve_review_item", finalise_first)
    (item,) = [i for i in repo.list_review_items(pack_run.run_id) if i.item_key == QTY_4419]
    resp = api_client_as("reviewer").post(f"{API}/review-items/{item.id}/resolve", json=ACCEPT)
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "RUN_STATE_CONFLICT"
    assert repo.get_review_item(item.id).status is ReviewStatus.OPEN
    actions = {e.action for e in repo.list_audit(run_id=pack_run.run_id)}
    assert AuditAction.REVIEW_RESOLVED not in actions


# --- I-7b, M-3: one audit read; items and count agree --------------------------------------


def _count_calls(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    calls: dict[str, int] = {}
    for name in dir(type(repo)):
        if name.startswith("_") or not callable(getattr(repo, name)):
            continue
        real = getattr(repo, name)

        def counted(*args: Any, _real: Any = real, _name: str = name, **kwargs: Any) -> Any:
            calls[_name] = calls.get(_name, 0) + 1
            return _real(*args, **kwargs)

        monkeypatch.setattr(repo, name, counted)
    return calls


def test_F08_R2_open_review_state_reads_audit_once(
    pack_run: PackRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _count_calls(pack_run.repo, monkeypatch)
    items, count = engine.open_review_state(pack_run.repo, pack_run.run_id)
    assert calls == {"list_review_items": 1, "list_audit": 1}
    assert count == len(items) == 4
    assert items == engine.open_review_items(pack_run.repo, pack_run.run_id)
    assert count == engine.open_review_count(pack_run.repo, pack_run.run_id)


def test_F08_R2_reconciled_audit_without_keys_items_and_count_agree(pack_run: PackRun) -> None:
    """A newest `RECONCILED` audit written before `open_item_keys` existed: the open items and
    the count come from the same fallback (every stored OPEN item: none can be ruled out)."""
    repo, run_id = pack_run.repo, pack_run.run_id
    repo.append_audit(
        make_audit(
            at=pack_run.run.scenario_now,
            run_id=run_id,
            action=AuditAction.RECONCILED,
            entity="run",
            entity_id=str(run_id),
            after={"status": "AWAITING_REVIEW", "open_review_items": 1},
            channel=AuditChannel.SYSTEM,
        )
    )
    stored_open = repo.list_review_items(run_id, ReviewStatus.OPEN)
    items, count = engine.open_review_state(repo, run_id)
    assert items == stored_open
    assert engine.open_review_count(repo, run_id) == count == len(items)
    assert engine.open_review_items(repo, run_id) == items


# --- I-7c, NFR-05: bounded repo calls ------------------------------------------------------

EP14_MAX_CALLS = 7  # 8 before the 005 final review (I-7b)
EP15_MAX_CALLS = 42  # 54 before the 005 final review (I-7c)


def test_F08_NFR05_ep14_repo_calls_bounded(
    pack_run: PackRun, api_client_as: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = api_client_as("reviewer")
    calls = _count_calls(pack_run.repo, monkeypatch)
    resp = client.get(f"{API}/runs/{pack_run.run_id}/review-items")
    assert resp.status_code == 200, resp.text
    assert sum(calls.values()) - calls.get("get_allowed_user", 0) <= EP14_MAX_CALLS, sorted(
        calls.items()
    )


def test_F08_NFR05_ep15_repo_calls_bounded(
    pack_run: PackRun, api_client_as: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = api_client_as("reviewer")
    (item,) = [
        i for i in pack_run.repo.list_review_items(pack_run.run_id) if i.item_key == QTY_4419
    ]
    calls = _count_calls(pack_run.repo, monkeypatch)
    resp = client.post(f"{API}/review-items/{item.id}/resolve", json=ACCEPT)
    assert resp.status_code == 200, resp.text
    assert sum(calls.values()) - calls.get("get_allowed_user", 0) <= EP15_MAX_CALLS, sorted(
        calls.items()
    )


# --- M-7: one channel rule for every route --------------------------------------------------


def test_F08_R4_routes_share_channel_and_read_errors() -> None:
    from app.routes import _common, review, runs

    for module in (review, runs):
        assert vars(module)["channel"] is _common.channel
        assert vars(module)["READ_ERRORS"] is _common.READ_ERRORS
    assert not hasattr(review, "_channel") and not hasattr(runs, "_channel")
