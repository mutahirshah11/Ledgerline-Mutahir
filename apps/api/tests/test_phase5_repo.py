"""006-web carry-overs from 004: batched Repo reads behind EP-04 and EP-05 (NFR-02).

Contract tests over every Repo implementation (`repo` fixture); the same tests run against
`SupabaseRepo` in `test_phase5_repo_integration.py`.
"""

from uuid import UUID

from app.core.db import Repo
from app.domain.enums import Outcome, ProofStatus, ReadVariant, RowKind, Source
from app.domain.models import CheckC2, Proof, ReconciliationPayload
from app.pipeline.engine import outcome_counts_of
from tests.factories import (
    make_extraction,
    make_group,
    make_payload,
    make_proof,
    make_row,
    make_run,
    sha,
)

# --- EP-05: extractions of many pages in one read ---------------------------------------------


def test_list_extractions_returns_every_read_of_the_pages_for_model_and_prompt(
    repo: Repo,
) -> None:
    wanted = [
        make_extraction(page_sha256=sha(7), read_variant=ReadVariant.B),
        make_extraction(page_sha256=sha(7), read_variant=ReadVariant.A),
        make_extraction(page_sha256=sha(8), read_variant=ReadVariant.A),
    ]
    others = [
        make_extraction(page_sha256=sha(7), prompt_version="v2"),
        make_extraction(page_sha256=sha(8), model="model-y"),
        make_extraction(page_sha256=sha(9)),
    ]
    for e in [*wanted, *others]:
        repo.put_extraction(e)
    got = repo.list_extractions((sha(8), sha(7)), "model-x", "v1")
    assert got == sorted(wanted, key=lambda e: (e.page_sha256, e.read_variant.value))
    assert repo.list_extractions((sha(10),), "model-x", "v1") == []
    assert repo.list_extractions((), "model-x", "v1") == []


# --- EP-04: outcome counts and proof statuses of many runs in one read ----------------------


def _reconcile(repo: Repo, n: int) -> ReconciliationPayload:
    """A reconciled run: payload `n` plus a DUPLICATE, a NON_RECEIPT and a MISSING group."""
    run = repo.create_run(make_run())
    base = make_payload(run.id, n)
    group = make_group(
        run.id, "GRN:99", grn_no=99, outcome=Outcome.MISSING, missing_from=[Source.SHEET]
    )
    rows = [
        *base.rows,
        make_row(run.id, "ERP-0091", outcome=Outcome.DUPLICATE, duplicate_of="ERP-0001"),
        make_row(run.id, "XLS-r91", outcome=Outcome.DUPLICATE),
        make_row(run.id, "XLS-r92", outcome=Outcome.NON_RECEIPT, row_kind=RowKind.NON_RECEIPT),
    ]
    payload = base.model_copy(update={"rows": rows, "groups": [*base.groups, group]})
    repo.replace_reconciliation(run.id, payload)
    return payload


def test_count_outcomes_matches_the_per_run_counts(repo: Repo) -> None:
    one, two = _reconcile(repo, 1), _reconcile(repo, 2)
    empty = repo.create_run(make_run())
    ids = [one.groups[0].run_id, empty.id, two.groups[0].run_id]
    got = repo.count_outcomes(ids)
    assert set(got) == set(ids)
    for run_id in ids:
        rows, groups = repo.list_source_rows(run_id), repo.list_groups(run_id)
        assert got[run_id] == outcome_counts_of(rows, groups).flat()
    assert got[ids[0]][Outcome.DUPLICATE] == 2
    assert got[ids[0]][Outcome.NON_RECEIPT] == 1
    assert got[ids[0]][Outcome.MISSING] == 1
    assert got[empty.id] == dict.fromkeys(Outcome, 0)
    assert repo.count_outcomes([]) == {}


def failed_proof(run_id: UUID) -> Proof:
    """A FAIL proof: check C2 finds an unplaced row."""
    proof = make_proof(run_id)
    c2 = CheckC2(pass_=False, rows=1, unplaced=["ERP-0001"], placed_twice=[])
    checks = proof.checks.model_copy(update={"C2": c2})
    return Proof(
        run_id=run_id, status=ProofStatus.FAIL, checks=checks, computed_at=proof.computed_at
    )


def test_list_proof_statuses_of_the_runs_with_a_proof(repo: Repo) -> None:
    passed, failed, none = (repo.create_run(make_run()) for _ in range(3))
    repo.save_proof(make_proof(passed.id))
    repo.save_proof(failed_proof(failed.id))
    got = repo.list_proof_statuses([passed.id, failed.id, none.id])
    assert got == {passed.id: ProofStatus.PASS, failed.id: ProofStatus.FAIL}
    assert repo.list_proof_statuses([]) == {}
