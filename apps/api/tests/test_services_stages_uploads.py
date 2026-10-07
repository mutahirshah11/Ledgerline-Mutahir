"""Upload stage services without HTTP (004 T028-T030; F-01.R1 to R7, FM-01, FM-02)."""

import hashlib
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pytest

from app.core.errors import ApiError, StateConflict
from app.core.memory_repo import MemoryRepo
from app.core.storage import INPUTS_BUCKET, MemoryBlobStore
from app.domain.enums import AuditAction, ErrorCode, Role, RunEventStatus, RunStatus, Source, Stage
from app.domain.ids import run_key
from app.domain.models import AllowedUser, Run
from app.services.stages import (
    UploadFileIn,
    confirm_uploads,
    create_run,
    request_uploads,
    run_outcome_counts,
)

USER = AllowedUser(email="controller@kestrel.example", display_name="C", role=Role.CONTROLLER)
KB = 1024
IMG1 = b"\xff\xd8" + b"1" * (30 * KB)
IMG2 = b"\x89PNG" + b"2" * (30 * KB)
ERP = b"grn_no,qty\r\n"
SHEET = b"PK-sheet"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pack() -> list[tuple[UploadFileIn, bytes]]:
    def f(source: Source, name: str, data: bytes, **over: Any) -> tuple[UploadFileIn, bytes]:
        spec = UploadFileIn(
            source=source, filename=name, size_bytes=len(data), sha256=_sha(data), **over
        )
        return spec, data

    return [
        f(Source.REGISTER, "p1.jpg", IMG1),
        f(Source.REGISTER, "p2.png", IMG2),
        f(Source.ERP, "erp.csv", ERP),
        f(Source.SHEET, "sheet.xlsx", SHEET),
    ]


@pytest.fixture
def blobs(frozen_clock: datetime) -> MemoryBlobStore:
    return MemoryBlobStore()


@pytest.fixture
def run(memory_repo: MemoryRepo) -> Run:
    return create_run(memory_repo, USER, "2026-09")


def _upload_all(
    repo: MemoryRepo, blobs: MemoryBlobStore, run: Run, pack: list[tuple[UploadFileIn, bytes]]
) -> None:
    slots = request_uploads(repo, blobs, run, [spec for spec, _ in pack])
    by_sha = {_sha(data): data for _, data in pack}
    for slot in slots:
        stored = next(f for f in repo.list_source_files(run.id) if f.id == slot.file_id)
        blobs.put(INPUTS_BUCKET, slot.storage_path, by_sha[stored.sha256], "application/x")


def _err(fn: Callable[[], object]) -> ApiError:
    with pytest.raises(ApiError) as caught:
        fn()
    return caught.value


# --- T028 create_run ------------------------------------------------------------------


def test_svc_create_run_created_with_clock_and_user(
    memory_repo: MemoryRepo, frozen_clock: datetime
) -> None:
    run = create_run(memory_repo, USER, "2026-09")
    assert run.status is RunStatus.CREATED
    assert run.scenario_now == frozen_clock == run.created_at
    assert run.created_by == USER.email
    assert memory_repo.get_run(run.id) == run


@pytest.mark.parametrize("month", ["2026-13", "26-09", "2026-00", "2026-9", ""])
def test_svc_create_run_bad_month(
    memory_repo: MemoryRepo, frozen_clock: datetime, month: str
) -> None:
    err = _err(lambda: create_run(memory_repo, USER, month))
    assert (err.code, err.http_status) == (ErrorCode.BAD_MONTH, 400)
    assert memory_repo.list_runs() == []


# --- T029 request_uploads -------------------------------------------------------------


def test_svc_uploads_slots_paths_pages_and_started_event(
    memory_repo: MemoryRepo, blobs: MemoryBlobStore, run: Run
) -> None:
    pack = _pack()
    slots = request_uploads(memory_repo, blobs, run, [s for s, _ in pack])
    paths = [s.storage_path for s in slots]
    assert paths == [
        f"{run.id}/register/p1-{_sha(IMG1)}.jpg",
        f"{run.id}/register/p2-{_sha(IMG2)}.png",
        f"{run.id}/erp.csv",
        f"{run.id}/sheet.xlsx",
    ]
    assert [s.page_no for s in slots] == [1, 2, None, None]
    assert all(s.signed_url for s in slots)
    files = memory_repo.list_source_files(run.id)
    assert {f.id for f in files} == {s.file_id for s in slots}
    events = memory_repo.list_run_events(run.id)
    assert [(e.stage, e.status) for e in events] == [(Stage.UPLOAD, RunEventStatus.STARTED)]


def test_svc_uploads_given_page_numbers_kept(
    memory_repo: MemoryRepo, blobs: MemoryBlobStore, run: Run
) -> None:
    pack = _pack()
    specs = [s for s, _ in pack]
    specs[0] = specs[0].model_copy(update={"page_no": 2})
    specs[1] = specs[1].model_copy(update={"page_no": 1})
    slots = request_uploads(memory_repo, blobs, run, specs)
    assert [s.page_no for s in slots[:2]] == [2, 1]


def test_svc_uploads_missing_source_400_nothing_written(
    memory_repo: MemoryRepo, blobs: MemoryBlobStore, run: Run
) -> None:
    specs = [s for s, _ in _pack() if s.source is not Source.SHEET]
    err = _err(lambda: request_uploads(memory_repo, blobs, run, specs))
    assert (err.code, err.http_status) == (ErrorCode.MISSING_SOURCE, 400)
    assert err.details["missing"] == ["SHEET"]
    assert memory_repo.list_source_files(run.id) == []
    assert memory_repo.list_run_events(run.id) == []


def _rules(repo: MemoryRepo, blobs: MemoryBlobStore, run: Run, specs: list[UploadFileIn]) -> Any:
    err = _err(lambda: request_uploads(repo, blobs, run, specs))
    assert (err.code, err.http_status) == (ErrorCode.CONTRACT_VIOLATION, 422)
    assert err.details["problem"] == "FILE_RULES"
    assert repo.list_source_files(run.id) == []
    return err.details["violations"]


def _messages(violations: list[dict[str, Any]]) -> list[str]:
    return [v["message"] for v in violations]


def test_svc_uploads_file_rules(memory_repo: MemoryRepo, blobs: MemoryBlobStore, run: Run) -> None:
    specs = [s for s, _ in _pack()]
    erp2 = specs[2].model_copy(update={"sha256": "e" * 64})
    assert _messages(_rules(memory_repo, blobs, run, [*specs, erp2])) == [
        "Exactly one ERP file is allowed, got 2"
    ]
    pages = [
        specs[0].model_copy(update={"sha256": f"{n:064x}", "page_no": None}) for n in range(11)
    ]
    assert _messages(_rules(memory_repo, blobs, run, [*pages, specs[2], specs[3]])) == [
        "At most 10 register pages, got 11"
    ]
    small = specs[0].model_copy(update={"size_bytes": 10 * KB})
    assert _rules(memory_repo, blobs, run, [small, *specs[1:]]) == [
        {"filename": "p1.jpg", "message": "Register image is smaller than 20 KB"}
    ]
    big_sheet = specs[3].model_copy(update={"size_bytes": 10 * KB * KB + 1})
    assert _rules(memory_repo, blobs, run, [*specs[:3], big_sheet]) == [
        {"filename": "sheet.xlsx", "message": "File is larger than 10 MB"}
    ]
    twin = specs[1].model_copy(update={"sha256": specs[0].sha256})
    assert _messages(_rules(memory_repo, blobs, run, [specs[0], twin, *specs[2:]])) == [
        "The same file is declared 2 times: p1.jpg, p2.png"
    ]
    wrong_ext = specs[2].model_copy(update={"filename": "erp.xlsx"})
    assert _rules(memory_repo, blobs, run, [*specs[:2], wrong_ext, specs[3]]) == [
        {"filename": "erp.xlsx", "message": "ERP file must be .csv"}
    ]
    same_page = [
        specs[0].model_copy(update={"page_no": 1}),
        specs[1].model_copy(update={"page_no": 1}),
    ]
    assert _messages(_rules(memory_repo, blobs, run, [*same_page, *specs[2:]])) == [
        "Page 1 is given 2 times"
    ]


def test_svc_uploads_twice_same_records(
    memory_repo: MemoryRepo, blobs: MemoryBlobStore, run: Run
) -> None:
    specs = [s for s, _ in _pack()]
    first = request_uploads(memory_repo, blobs, run, specs)
    second = request_uploads(memory_repo, blobs, run, specs)
    assert [s.file_id for s in first] == [s.file_id for s in second]
    assert len(memory_repo.list_source_files(run.id)) == 4


def test_svc_uploads_again_with_other_file_replaces_stale_record(
    memory_repo: MemoryRepo, blobs: MemoryBlobStore, run: Run
) -> None:
    specs = [s for s, _ in _pack()]
    request_uploads(memory_repo, blobs, run, specs)
    other = specs[3].model_copy(update={"sha256": "a" * 64})
    request_uploads(memory_repo, blobs, run, [*specs[:3], other])
    shas = {f.sha256 for f in memory_repo.list_source_files(run.id)}
    assert len(shas) == 4 and "a" * 64 in shas and specs[3].sha256 not in shas


def test_svc_uploads_again_deletes_existing_object_before_new_url(
    memory_repo: MemoryRepo, blobs: MemoryBlobStore, run: Run
) -> None:
    """Signed upload URLs are `upsert=false`: a re-request for a path that already holds an
    object (a fixed ERP/sheet path, or a kept register file) must clear it first, or the
    browser's PUT gets a 409. `MemoryBlobStore` does not enforce no-overwrite itself, so this
    asserts the deletion directly."""
    specs = [s for s, _ in _pack()]
    first = request_uploads(memory_repo, blobs, run, specs)
    erp_slot = next(s for s in first if s.source is Source.ERP)
    blobs.put(INPUTS_BUCKET, erp_slot.storage_path, ERP, "text/csv")
    assert blobs.exists(INPUTS_BUCKET, erp_slot.storage_path)
    request_uploads(memory_repo, blobs, run, specs)
    assert not blobs.exists(INPUTS_BUCKET, erp_slot.storage_path)


def test_svc_uploads_after_created_409(
    memory_repo: MemoryRepo, blobs: MemoryBlobStore, run: Run
) -> None:
    pack = _pack()
    _upload_all(memory_repo, blobs, run, pack)
    confirmed = confirm_uploads(memory_repo, blobs, run, USER).run
    with pytest.raises(StateConflict):
        request_uploads(memory_repo, blobs, confirmed, [s for s, _ in pack])


# --- T030 confirm_uploads -------------------------------------------------------------


def _finalised_run(repo: MemoryRepo, key: str) -> Run:
    """A FINALISED run holding `key` (set on the move to UPLOADED; FINALISED is read-only)."""
    done = create_run(repo, USER, "2026-09")
    repo.transition_run(done.id, RunStatus.UPLOADED, expected={RunStatus.CREATED}, run_key=key)
    for to in (
        RunStatus.VALIDATED,
        RunStatus.EXTRACTING,
        RunStatus.EXTRACTED,
        RunStatus.RECONCILED,
        RunStatus.READY,
        RunStatus.FINALISED,
    ):
        done = repo.transition_run(done.id, to, expected={repo.get_run(done.id).status})
    return done


def test_svc_confirm_uploaded_with_key_and_pages(
    memory_repo: MemoryRepo, blobs: MemoryBlobStore, run: Run, frozen_clock: datetime
) -> None:
    pack = _pack()
    _upload_all(memory_repo, blobs, run, pack)
    result = confirm_uploads(memory_repo, blobs, run, USER)
    assert result.run.status is RunStatus.UPLOADED
    assert result.run.run_key == run_key("2026-09", [_sha(d) for _, d in pack])
    assert (result.new_pages, result.known_pages) == ([1, 2], [])
    assert all(f.confirmed_at == frozen_clock for f in memory_repo.list_source_files(run.id))
    page = memory_repo.get_page(_sha(IMG1))
    assert page is not None and page.first_seen_run_id == run.id and page.page_no == 1
    statuses = sorted(e.status for e in memory_repo.list_run_events(run.id))
    assert statuses == [RunEventStatus.OK, RunEventStatus.STARTED]  # frozen clock: same `at`


def test_svc_confirm_audit_once_and_idempotent(
    memory_repo: MemoryRepo, blobs: MemoryBlobStore, run: Run
) -> None:
    _upload_all(memory_repo, blobs, run, _pack())
    first = confirm_uploads(memory_repo, blobs, run, USER)
    again = confirm_uploads(memory_repo, blobs, memory_repo.get_run(run.id), USER)
    assert again == first
    actions = sorted(a.action for a in memory_repo.list_audit(run_id=run.id))
    assert actions == [AuditAction.FILE_UPLOADED] * 4 + [AuditAction.RUN_CREATED]
    assert {a.actor for a in memory_repo.list_audit(run_id=run.id)} == {USER.email}


def test_svc_confirm_second_run_known_pages(
    memory_repo: MemoryRepo, blobs: MemoryBlobStore, run: Run
) -> None:
    pack = _pack()
    _upload_all(memory_repo, blobs, run, pack)
    confirm_uploads(memory_repo, blobs, run, USER)
    second = create_run(memory_repo, USER, "2026-09")
    _upload_all(memory_repo, blobs, second, pack)
    result = confirm_uploads(memory_repo, blobs, second, USER)
    assert (result.new_pages, result.known_pages) == ([], [1, 2])


def test_svc_confirm_hash_mismatch_deletes_object_keeps_row(
    memory_repo: MemoryRepo, blobs: MemoryBlobStore, run: Run
) -> None:
    pack = _pack()
    _upload_all(memory_repo, blobs, run, pack)
    erp = next(f for f in memory_repo.list_source_files(run.id) if f.source is Source.ERP)
    blobs.put(INPUTS_BUCKET, erp.storage_path, b"other bytes", "text/csv")
    err = _err(lambda: confirm_uploads(memory_repo, blobs, run, USER))
    assert (err.code, err.http_status) == (ErrorCode.CONTRACT_VIOLATION, 422)
    assert err.details["problem"] == "HASH_MISMATCH"
    assert [f["file_id"] for f in err.details["files"]] == [str(erp.id)]
    assert not blobs.exists(INPUTS_BUCKET, erp.storage_path)  # FM-02: object deleted
    assert erp in memory_repo.list_source_files(run.id)  # the row is kept
    assert memory_repo.get_run(run.id).status is RunStatus.CREATED
    assert RunEventStatus.FAILED in {e.status for e in memory_repo.list_run_events(run.id)}
    # a retry without re-uploading names the file as missing
    err = _err(lambda: confirm_uploads(memory_repo, blobs, run, USER))
    assert err.details["problem"] == "FILE_MISSING"
    assert [f["file_id"] for f in err.details["files"]] == [str(erp.id)]
    blobs.put(INPUTS_BUCKET, erp.storage_path, ERP, "text/csv")
    assert confirm_uploads(memory_repo, blobs, run, USER).run.status is RunStatus.UPLOADED


def test_svc_confirm_finalised_run_409(
    memory_repo: MemoryRepo, blobs: MemoryBlobStore, run: Run
) -> None:
    done = _finalised_run(memory_repo, "f" * 64)
    with pytest.raises(StateConflict):
        confirm_uploads(memory_repo, blobs, done, USER)


def test_svc_confirm_lost_race_answers_as_repeat_without_audit(
    memory_repo: MemoryRepo,
    blobs: MemoryBlobStore,
    run: Run,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent confirm moves the run first; this one gets StateConflict on the CAS."""
    _upload_all(memory_repo, blobs, run, _pack())
    real = memory_repo.transition_run

    def racing(run_id: Any, to: RunStatus, **kw: Any) -> Run:
        current = real(run_id, to, **kw)  # the other request wins
        raise StateConflict(current)

    monkeypatch.setattr(memory_repo, "transition_run", racing)
    result = confirm_uploads(memory_repo, blobs, run, USER)
    assert result.run.status is RunStatus.UPLOADED
    assert (result.new_pages, result.known_pages) == ([1, 2], [])
    assert memory_repo.list_audit(run_id=run.id) == []


def test_svc_confirm_missing_object(
    memory_repo: MemoryRepo, blobs: MemoryBlobStore, run: Run
) -> None:
    request_uploads(memory_repo, blobs, run, [s for s, _ in _pack()])
    err = _err(lambda: confirm_uploads(memory_repo, blobs, run, USER))
    assert err.details["problem"] == "FILE_MISSING"
    assert len(err.details["files"]) == 4
    assert memory_repo.get_run(run.id).status is RunStatus.CREATED
    assert len(memory_repo.list_source_files(run.id)) == 4


def test_svc_confirm_window_expired(
    memory_repo: MemoryRepo, blobs: MemoryBlobStore, run: Run, frozen_clock: datetime
) -> None:
    _upload_all(memory_repo, blobs, run, _pack())
    later = frozen_clock + timedelta(minutes=11)
    err = _err(lambda: confirm_uploads(memory_repo, blobs, run, USER, now=lambda: later))
    assert err.details["problem"] == "UPLOAD_EXPIRED"
    assert memory_repo.get_run(run.id).status is RunStatus.CREATED
    ok = confirm_uploads(
        memory_repo, blobs, run, USER, now=lambda: frozen_clock + timedelta(minutes=10)
    )
    assert ok.run.status is RunStatus.UPLOADED


def test_svc_confirm_duplicate_finalised_run(
    memory_repo: MemoryRepo, blobs: MemoryBlobStore, run: Run
) -> None:
    pack = _pack()
    done = _finalised_run(memory_repo, run_key("2026-09", [_sha(d) for _, d in pack]))
    _upload_all(memory_repo, blobs, run, pack)
    err = _err(lambda: confirm_uploads(memory_repo, blobs, run, USER))
    assert (err.code, err.http_status) == (ErrorCode.DUPLICATE_RUN, 409)
    assert err.details == {"run_id": str(done.id)}
    assert memory_repo.get_run(run.id).status is RunStatus.CREATED


# --- OutcomeCounts (data-model.md §2) -------------------------------------------------


def test_svc_outcome_counts_groups_and_row_only_outcomes(memory_repo: MemoryRepo, run: Run) -> None:
    from app.domain.enums import Outcome, RowKind
    from app.domain.models import ReconciliationPayload
    from tests.factories import make_group, make_row

    groups = [
        make_group(run.id, "GRN:1"),
        make_group(run.id, "GRN:2"),
        make_group(run.id, "GRN:3", outcome=Outcome.MISSING, missing_from=[Source.ERP]),
    ]
    rows = [
        make_row(run.id, "XLS-r2", row_kind=RowKind.DUPLICATE, outcome=Outcome.DUPLICATE),
        make_row(run.id, "XLS-r3", row_kind=RowKind.NON_RECEIPT, outcome=Outcome.NON_RECEIPT),
        make_row(run.id, "ERP-0001", group_id=groups[0].id, outcome=Outcome.MATCHED),
    ]
    memory_repo.replace_reconciliation(
        run.id, ReconciliationPayload(rows=rows, groups=groups, flags=[], norm_log=[])
    )
    counts = run_outcome_counts(memory_repo, run.id)
    assert set(counts) == set(Outcome)
    assert counts[Outcome.MATCHED] == 2 and counts[Outcome.MISSING] == 1
    assert counts[Outcome.DUPLICATE] == 1 and counts[Outcome.NON_RECEIPT] == 1
    assert counts[Outcome.MISMATCHED] == 0


def test_svc_engine_counts_flat_projection(memory_repo: MemoryRepo, run: Run) -> None:
    """data-model.md §2: group outcomes count groups; DUPLICATE and NON_RECEIPT count rows."""
    from app.domain.enums import Outcome, RowKind
    from app.pipeline.engine import outcome_counts_of
    from tests.factories import make_group, make_row

    groups = [make_group(run.id, "GRN:1"), make_group(run.id, "GRN:2", outcome=Outcome.MISMATCHED)]
    rows = [
        make_row(run.id, "REG-p1-r01", group_id=groups[0].id, outcome=Outcome.MATCHED),
        make_row(run.id, "ERP-0001", group_id=groups[0].id, outcome=Outcome.MATCHED),
        make_row(run.id, "XLS-r2", row_kind=RowKind.DUPLICATE, outcome=Outcome.DUPLICATE),
        make_row(run.id, "ERP-0002", row_kind=RowKind.DUPLICATE, outcome=Outcome.DUPLICATE),
    ]
    flat = outcome_counts_of(rows, groups).flat()
    assert flat == dict.fromkeys(Outcome, 0) | {
        Outcome.MATCHED: 1,
        Outcome.MISMATCHED: 1,
        Outcome.DUPLICATE: 2,
    }
