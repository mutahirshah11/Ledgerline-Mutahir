"""The 2026-09 pack run reconciled through the Phase 3 stage services, before review (005 T011).

Uploads the four pack files (EP-06/EP-07 services), validates (EP-08), extracts both pages from
the committed reads stored in the repo (EP-09; the model is never called) and reconciles
(EP-10 `reconcile_stage`), so the run, its files, blobs, events, audit and review items are
exactly what the API would have written. Asserts the blueprint Section 8 "before review" counts
(21 MATCHED, 4 MISMATCHED, 2 MISSING, 2 NEEDS_REVIEW) and proof PASS.
"""

import copy
import hashlib
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from app.core import clock
from app.core.config import Settings
from app.core.db import Repo
from app.core.memory_repo import MemoryRepo
from app.core.storage import INPUTS_BUCKET, BlobStore, MemoryBlobStore
from app.domain.enums import Outcome, ProofStatus, ReadVariant, Role, RunStatus, Source
from app.domain.models import AllowedUser, Extraction, Run
from app.pipeline.model_client import RawRead
from app.services import stages
from app.services.telegram import Notifier
from tests.fakes.api import FakeNotifier

SCENARIO_NOW = datetime(2026, 10, 15, 8, 30, tzinfo=clock.KARACHI)
"""The scenario instant: conftest `frozen_clock` and `frozen_scenario_clock` freeze here."""
PACK_UPLOADS = (
    ("t4_register_page1.jpg", Source.REGISTER, 1),
    ("t4_register_page2.jpg", Source.REGISTER, 2),
    ("t4_erp_grn.csv", Source.ERP, None),
    ("t4_store_sheet.xlsx", Source.SHEET, None),
)
BEFORE_REVIEW = {
    Outcome.MATCHED: 21,
    Outcome.MISMATCHED: 4,
    Outcome.MISSING: 2,
    Outcome.NEEDS_REVIEW: 2,
}
"""Blueprint Section 8, pack before review (the groups this phase acts on)."""
CONTROLLER = AllowedUser(
    email="controller@kestrel.example", display_name="Controller (Demo)", role=Role.CONTROLLER
)
"""The seeded controller (`SEEDED_USERS` in conftest), who creates and runs the pack run."""


@dataclass(frozen=True)
class PackRun:
    """The reconciled pack run and where it lives."""

    run: Run
    repo: Repo
    blobs: BlobStore
    result: stages.ReconcileResult

    @property
    def run_id(self) -> UUID:
        return self.run.id


class _NoModel:
    """The page reads are already stored, so `extract_page` must never reach a model."""

    def read_page(
        self,
        *,
        image: bytes,
        mime: str,
        variant: ReadVariant,
        instructions: str,
        schema: dict[str, Any],
    ) -> RawRead:
        raise AssertionError("pack run reads only stored extractions")


def build_reconciled_pack_run(
    repo: Repo,
    store: BlobStore,
    *,
    pack_dir: Path,
    reads: Sequence[Extraction],
    notifier: Notifier | None = None,
    now: Callable[[], datetime] = clock.now,
) -> PackRun:
    """The pack run in `repo` / `store`, `AWAITING_REVIEW` after reconcile (see module doc).

    `reads` are the committed extractions (conftest `cached_extraction_records`); they are
    stored in `repo` first. `notifier` defaults to a `FakeNotifier` (nothing is sent on the
    happy path). Uses `now` for every timestamp (pass nothing under `frozen_clock`)."""
    notifier = notifier or FakeNotifier()
    user = repo.upsert_allowed_user(CONTROLLER)
    for record in reads:
        repo.put_extraction(record)
    run = stages.create_run(repo, user, "2026-09", now=now)
    data = {name: (pack_dir / name).read_bytes() for name, _, _ in PACK_UPLOADS}
    declared = [
        stages.UploadFileIn(
            source=source,
            filename=name,
            size_bytes=len(data[name]),
            sha256=hashlib.sha256(data[name]).hexdigest(),
            page_no=page,
        )
        for name, source, page in PACK_UPLOADS
    ]
    for slot in stages.request_uploads(repo, store, run, declared, now=now):
        store.put(INPUTS_BUCKET, slot.storage_path, data[slot.filename], "application/octet-stream")
    run = stages.confirm_uploads(repo, store, run, user, notifier=notifier, now=now).run
    problems = stages.validate_stage(repo, store, notifier, run, user, now=now)
    assert problems == [], problems
    for page in (1, 2):
        stages.extract_stage(
            repo,
            store,
            notifier,
            repo.get_run(run.id),
            page,
            user,
            None,
            client=_NoModel(),
            now=now,
        )
    result = stages.reconcile_stage(
        repo, store, notifier, repo.get_run(run.id), user, None, now=now
    )
    run = repo.get_run(run.id)
    assert run.status is RunStatus.AWAITING_REVIEW, run
    assert result.proof_status is ProofStatus.PASS
    assert {o: result.counts[o] for o in BEFORE_REVIEW} == BEFORE_REVIEW, result.counts
    return PackRun(run=run, repo=repo, blobs=store, result=result)


def clone_pack_run(template: PackRun, repo: MemoryRepo, blobs: MemoryBlobStore) -> PackRun:
    """`template` (built once per session into a MemoryRepo / MemoryBlobStore) copied into
    `repo` and `blobs` (empty but for allowed users), so tests never share state. Each store
    container is copied one level deep: MemoryRepo keeps only frozen models, never mutates a
    stored one (it replaces it, and a run's reconcile outputs as a unit) and hands out deep
    copies, so sharing the models is safe. Well under 1 ms versus ~350 ms per build (NFR-11)."""
    assert isinstance(template.repo, MemoryRepo) and isinstance(template.blobs, MemoryBlobStore)
    users = dict(repo._users)  # e.g. the allowlist the `client` fixture seeded
    state = {k: copy.copy(v) for k, v in vars(template.repo).items() if k != "_lock"}
    vars(repo).update(state)
    repo._users.update(users)
    blobs._objects.update(template.blobs._objects)  # bytes are immutable
    return replace(template, repo=repo, blobs=blobs)


@contextmanager
def frozen_scenario_clock() -> Iterator[datetime]:
    """`app.core.clock` frozen at the scenario instant (as conftest `frozen_clock`), for a
    session-scoped build; undone on exit."""
    settings = Settings(
        _env_file=None,
        LEDGERLINE_NOW=SCENARIO_NOW,
        LEDGERLINE_CLOCK_MODE="frozen",
        LEDGERLINE_NOW_ANCHOR=None,
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(clock, "get_settings", lambda: settings)
        yield clock.now()
