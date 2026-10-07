"""F-01 runs and uploads over HTTP (004 T027; EP-03 to EP-07, FM-01, FM-02, C17, C19, C25)."""

import hashlib
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.memory_repo import MemoryRepo
from app.core.storage import INPUTS_BUCKET, MemoryBlobStore
from app.domain.enums import (
    AuditAction,
    Outcome,
    ProofStatus,
    ReadVariant,
    RowKind,
    RunEventStatus,
    RunStatus,
    Source,
    Stage,
)
from app.domain.ids import run_key
from app.domain.models import ReconciliationPayload
from tests.factories import (
    make_event,
    make_extraction,
    make_group,
    make_proof,
    make_review,
    make_row,
)

API = "/api/v1"
Upload = Callable[..., dict[str, Any]]


def _error(resp: Any) -> dict[str, Any]:
    body: dict[str, Any] = resp.json()["error"]
    return body


def _create(client: TestClient, headers: dict[str, str], month: str = "2026-09") -> str:
    resp = client.post(f"{API}/runs", json={"month": month}, headers=headers)
    assert resp.status_code == 201, resp.text
    run_id: str = resp.json()["id"]
    return run_id


def _slots(
    client: TestClient, headers: dict[str, str], run_id: str, files: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    resp = client.post(f"{API}/runs/{run_id}/uploads", json={"files": files}, headers=headers)
    assert resp.status_code == 200, resp.text
    slots: list[dict[str, Any]] = resp.json()["uploads"]
    return slots


def _put_all(blobs: MemoryBlobStore, slots: list[dict[str, Any]], files: dict[str, bytes]) -> None:
    for slot in slots:
        blobs.put(INPUTS_BUCKET, slot["storage_path"], files[slot["filename"]], "x/y")


def _confirm(client: TestClient, headers: dict[str, str], run_id: str) -> Any:
    return client.post(f"{API}/runs/{run_id}/uploads/confirm", headers=headers)


MoveClock = Callable[[datetime], None]


# --- EP-03, EP-06, EP-07 --------------------------------------------------------------


def test_F01_AC1_pack_upload_creates_run(
    pack_upload: Upload, memory_repo: MemoryRepo, pack_declared: list[dict[str, Any]]
) -> None:
    body = pack_upload()
    run = body["run"]
    assert run["status"] == "UPLOADED"
    assert run["run_key"] == run_key("2026-09", [f["sha256"] for f in pack_declared])
    assert (body["new_pages"], body["known_pages"]) == ([1, 2], [])
    runs = memory_repo.list_runs()
    assert [str(r.id) for r in runs] == [run["id"]]
    files = memory_repo.list_source_files(runs[0].id)
    assert len(files) == 4 and all(f.confirmed_at is not None for f in files)


def test_F01_AC2_missing_sheet_no_objects(
    client: TestClient,
    tokens: dict[str, dict[str, str]],
    memory_repo: MemoryRepo,
    memory_blobs: MemoryBlobStore,
    pack_declared: list[dict[str, Any]],
) -> None:
    run_id = _create(client, tokens["controller"])
    files = [f for f in pack_declared if f["source"] != "SHEET"]
    resp = client.post(
        f"{API}/runs/{run_id}/uploads", json={"files": files}, headers=tokens["controller"]
    )
    assert resp.status_code == 400
    assert _error(resp)["code"] == "MISSING_SOURCE"
    assert memory_repo.list_source_files(UUID(run_id)) == []
    paths = [f"{run_id}/erp.csv", f"{run_id}/sheet.xlsx"] + [
        f"{run_id}/register/p{n}-{f['sha256']}.jpg" for n, f in enumerate(files[:2], 1)
    ]
    assert not any(memory_blobs.exists(INPUTS_BUCKET, path) for path in paths)


def test_F01_AC3_wrong_hash_422_stays_created(
    client: TestClient,
    tokens: dict[str, dict[str, str]],
    memory_repo: MemoryRepo,
    memory_blobs: MemoryBlobStore,
    pack_files: dict[str, bytes],
    pack_declared: list[dict[str, Any]],
) -> None:
    headers = tokens["controller"]
    run_id = _create(client, headers)
    wrong = [dict(f) for f in pack_declared]
    wrong[2]["sha256"] = hashlib.sha256(b"not the erp").hexdigest()
    slots = _slots(client, headers, run_id, wrong)
    _put_all(memory_blobs, slots, pack_files)
    resp = _confirm(client, headers, run_id)
    assert resp.status_code == 422
    err = _error(resp)
    assert err["code"] == "CONTRACT_VIOLATION"
    assert err["details"]["problem"] == "HASH_MISMATCH"
    assert [f["filename"] for f in err["details"]["files"]] == ["t4_erp_grn.csv"]
    assert not memory_blobs.exists(INPUTS_BUCKET, slots[2]["storage_path"])
    # FM-02: object deleted, file row kept
    assert slots[2]["file_id"] in {str(f.id) for f in memory_repo.list_source_files(UUID(run_id))}
    assert memory_repo.get_run(UUID(run_id)).status is RunStatus.CREATED


def test_F01_FM02_retry_confirm_without_reupload_422(
    client: TestClient,
    tokens: dict[str, dict[str, str]],
    memory_repo: MemoryRepo,
    memory_blobs: MemoryBlobStore,
    pack_files: dict[str, bytes],
    pack_declared: list[dict[str, Any]],
) -> None:
    headers = tokens["controller"]
    run_id = _create(client, headers)
    slots = _slots(client, headers, run_id, pack_declared)
    _put_all(memory_blobs, slots, pack_files)
    page1 = slots[0]
    memory_blobs.put(INPUTS_BUCKET, page1["storage_path"], b"another photo", "image/jpeg")
    first = _error(_confirm(client, headers, run_id))
    assert first["details"]["problem"] == "HASH_MISMATCH"
    assert [f["file_id"] for f in first["details"]["files"]] == [page1["file_id"]]
    resp = _confirm(client, headers, run_id)  # retried without re-uploading page 1
    assert resp.status_code == 422
    retry = _error(resp)
    assert retry["details"]["problem"] == "FILE_MISSING"
    assert [f["file_id"] for f in retry["details"]["files"]] == [page1["file_id"]]
    assert len(memory_repo.list_source_files(UUID(run_id))) == 4
    assert memory_repo.get_run(UUID(run_id)).status is RunStatus.CREATED
    memory_blobs.put(INPUTS_BUCKET, page1["storage_path"], pack_files[page1["filename"]], "x/y")
    body = _confirm(client, headers, run_id).json()
    assert body["new_pages"] == [1, 2]  # no page silently dropped


def test_F01_EP07_finalised_run_409(
    client: TestClient, tokens: dict[str, dict[str, str]], memory_repo: MemoryRepo
) -> None:
    from tests.factories import make_run

    done = memory_repo.create_run(make_run(status=RunStatus.FINALISED, run_key="f" * 64))
    resp = _confirm(client, tokens["controller"], str(done.id))
    assert resp.status_code == 409
    assert _error(resp)["code"] == "RUN_STATE_CONFLICT"
    assert _error(resp)["details"]["status"] == "FINALISED"


def test_F01_AC4_second_run_known_pages(pack_upload: Upload) -> None:
    pack_upload()
    second = pack_upload()
    assert (second["new_pages"], second["known_pages"]) == ([], [1, 2])


def test_F01_AC5_duplicate_finalised_run_409(
    client: TestClient,
    tokens: dict[str, dict[str, str]],
    memory_repo: MemoryRepo,
    memory_blobs: MemoryBlobStore,
    pack_files: dict[str, bytes],
    pack_declared: list[dict[str, Any]],
) -> None:
    from tests.factories import make_run

    key = run_key("2026-09", [f["sha256"] for f in pack_declared])
    done = memory_repo.create_run(make_run(status=RunStatus.FINALISED, run_key=key))
    headers = tokens["controller"]
    run_id = _create(client, headers)
    _put_all(memory_blobs, _slots(client, headers, run_id, pack_declared), pack_files)
    resp = _confirm(client, headers, run_id)
    assert resp.status_code == 409
    assert _error(resp)["code"] == "DUPLICATE_RUN"
    assert _error(resp)["details"] == {"run_id": str(done.id)}
    assert memory_repo.get_run(UUID(run_id)).status is RunStatus.CREATED


def test_F01_FM01_missing_object_422(
    client: TestClient,
    tokens: dict[str, dict[str, str]],
    memory_repo: MemoryRepo,
    memory_blobs: MemoryBlobStore,
    pack_files: dict[str, bytes],
    pack_declared: list[dict[str, Any]],
) -> None:
    headers = tokens["controller"]
    run_id = _create(client, headers)
    slots = _slots(client, headers, run_id, pack_declared)
    _put_all(memory_blobs, slots[1:], pack_files)  # page 1 never uploaded
    resp = _confirm(client, headers, run_id)
    assert resp.status_code == 422
    err = _error(resp)
    assert (err["code"], err["details"]["problem"]) == ("CONTRACT_VIOLATION", "FILE_MISSING")
    assert [f["filename"] for f in err["details"]["files"]] == ["t4_register_page1.jpg"]
    assert memory_repo.get_run(UUID(run_id)).status is RunStatus.CREATED


def test_F01_FM01_upload_window_expired_422(
    client: TestClient,
    tokens: dict[str, dict[str, str]],
    memory_repo: MemoryRepo,
    memory_blobs: MemoryBlobStore,
    pack_files: dict[str, bytes],
    pack_declared: list[dict[str, Any]],
    frozen_clock: datetime,
    move_clock: MoveClock,
) -> None:
    headers = tokens["controller"]
    run_id = _create(client, headers)
    _put_all(memory_blobs, _slots(client, headers, run_id, pack_declared), pack_files)
    move_clock(frozen_clock + timedelta(minutes=11))
    resp = _confirm(client, headers, run_id)
    assert resp.status_code == 422
    assert _error(resp)["details"]["problem"] == "UPLOAD_EXPIRED"
    assert memory_repo.get_run(UUID(run_id)).status is RunStatus.CREATED
    # fresh links restart the window
    _put_all(memory_blobs, _slots(client, headers, run_id, pack_declared), pack_files)
    assert _confirm(client, headers, run_id).status_code == 200


@pytest.mark.parametrize("month", ["2026-13", "26-09"])
def test_F01_R1_bad_month_400(
    client: TestClient, tokens: dict[str, dict[str, str]], memory_repo: MemoryRepo, month: str
) -> None:
    resp = client.post(f"{API}/runs", json={"month": month}, headers=tokens["controller"])
    assert resp.status_code == 400
    assert _error(resp)["code"] == "BAD_MONTH"
    assert memory_repo.list_runs() == []


def test_F01_R1_create_run_fields(
    client: TestClient, tokens: dict[str, dict[str, str]], frozen_clock: datetime
) -> None:
    resp = client.post(f"{API}/runs", json={"month": "2026-09"}, headers=tokens["controller"])
    run = resp.json()
    assert (run["status"], run["created_by"]) == ("CREATED", "controller@kestrel.example")
    assert datetime.fromisoformat(run["scenario_now"]) == frozen_clock
    assert run["run_key"] is None


def test_F01_R3_file_rules_422(
    client: TestClient,
    tokens: dict[str, dict[str, str]],
    memory_repo: MemoryRepo,
    pack_declared: list[dict[str, Any]],
) -> None:
    headers = tokens["controller"]
    run_id = _create(client, headers)
    page, erp = pack_declared[0], pack_declared[2]
    pages = [dict(page, sha256=f"{n:064x}") for n in range(11)]
    cases = {
        "two ERP files": [*pack_declared, dict(erp, sha256="e" * 64)],
        "11 pages": [*pages, *pack_declared[2:]],
        "10 KB image": [dict(page, size_bytes=10 * 1024), *pack_declared[1:]],
        "duplicate hashes": [
            page,
            dict(pack_declared[1], sha256=page["sha256"]),
            *pack_declared[2:],
        ],
    }
    for name, files in cases.items():
        resp = client.post(f"{API}/runs/{run_id}/uploads", json={"files": files}, headers=headers)
        assert resp.status_code == 422, name
        err = _error(resp)
        assert (err["code"], err["details"]["problem"]) == ("CONTRACT_VIOLATION", "FILE_RULES")
        assert err["details"]["violations"], name
    assert memory_repo.list_source_files(UUID(run_id)) == []


def test_F01_R7_audit_run_created_and_files_uploaded(
    pack_upload: Upload, memory_repo: MemoryRepo
) -> None:
    run_id = UUID(pack_upload()["run"]["id"])
    entries = memory_repo.list_audit(run_id=run_id)
    actions = sorted(e.action for e in entries)
    assert actions == [AuditAction.FILE_UPLOADED] * 4 + [AuditAction.RUN_CREATED]
    assert {(e.actor, e.channel.value) for e in entries} == {("controller@kestrel.example", "WEB")}


def test_F01_confirm_twice_idempotent_no_second_audit(
    pack_upload: Upload,
    client: TestClient,
    tokens: dict[str, dict[str, str]],
    memory_repo: MemoryRepo,
) -> None:
    first = pack_upload()
    run_id = first["run"]["id"]
    again = _confirm(client, tokens["controller"], run_id)
    assert again.status_code == 200
    assert again.json() == first
    assert len(memory_repo.list_audit(run_id=UUID(run_id))) == 5


def test_F01_uploads_twice_same_records_fresh_urls(
    client: TestClient,
    tokens: dict[str, dict[str, str]],
    memory_repo: MemoryRepo,
    pack_declared: list[dict[str, Any]],
) -> None:
    headers = tokens["controller"]
    run_id = _create(client, headers)
    first = _slots(client, headers, run_id, pack_declared)
    second = _slots(client, headers, run_id, pack_declared)
    assert [s["file_id"] for s in first] == [s["file_id"] for s in second]
    assert all(s["signed_url"] for s in second)
    assert len(memory_repo.list_source_files(UUID(run_id))) == 4
    started = [
        e
        for e in memory_repo.list_run_events(UUID(run_id))
        if (e.stage, e.status) == (Stage.UPLOAD, RunEventStatus.STARTED)
    ]
    assert len(started) == 2


def test_F01_stage_endpoints_controller_only(
    client: TestClient, tokens: dict[str, dict[str, str]], pack_declared: list[dict[str, Any]]
) -> None:
    run_id = _create(client, tokens["controller"])
    for who in ("reviewer", "ops"):
        headers = tokens[who]
        assert (
            client.post(f"{API}/runs", json={"month": "2026-09"}, headers=headers).status_code
            == 403
        )
        resp = client.post(
            f"{API}/runs/{run_id}/uploads", json={"files": pack_declared}, headers=headers
        )
        assert resp.status_code == 403
        assert _confirm(client, headers, run_id).status_code == 403


# --- EP-04, EP-05 ---------------------------------------------------------------------


def _reconciled(repo: MemoryRepo, run_id: UUID) -> None:
    """Two MATCHED groups, one MISSING, one DUPLICATE row; proof PASS; one open item."""
    groups = [
        make_group(run_id, "GRN:1"),
        make_group(run_id, "GRN:2"),
        make_group(run_id, "GRN:3", outcome=Outcome.MISSING, missing_from=[Source.ERP]),
    ]
    rows = [make_row(run_id, "XLS-r2", row_kind=RowKind.DUPLICATE, outcome=Outcome.DUPLICATE)]
    repo.replace_reconciliation(
        run_id, ReconciliationPayload(rows=rows, groups=groups, flags=[], norm_log=[])
    )
    repo.save_proof(make_proof(run_id, status=ProofStatus.PASS))
    repo.upsert_review_items(run_id, [make_review(run_id, "CELL:REG-p1-r01:qty")])


def test_F01_EP04_list_newest_first_with_counts(
    pack_upload: Upload,
    client: TestClient,
    tokens: dict[str, dict[str, str]],
    memory_repo: MemoryRepo,
    frozen_clock: datetime,
    move_clock: MoveClock,
) -> None:
    older = UUID(pack_upload()["run"]["id"])
    _reconciled(memory_repo, older)
    move_clock(frozen_clock + timedelta(minutes=1))
    newer = _create(client, tokens["controller"], "2026-10")
    resp = client.get(f"{API}/runs", headers=tokens["reviewer"])
    assert resp.status_code == 200
    body = resp.json()
    assert [r["id"] for r in body] == [newer, str(older)]
    assert set(body[0]["counts"]) == {o.value for o in Outcome}
    assert sum(body[0]["counts"].values()) == 0 and body[0]["proof_status"] is None
    counts = body[1]["counts"]
    assert (counts["MATCHED"], counts["MISSING"], counts["DUPLICATE"]) == (2, 1, 1)
    assert body[1]["proof_status"] == "PASS" and body[1]["status"] == "UPLOADED"
    limited = client.get(f"{API}/runs?limit=1", headers=tokens["ops"]).json()
    assert [r["id"] for r in limited] == [newer]


def test_F01_EP05_detail_with_stage_progress(
    pack_upload: Upload,
    client: TestClient,
    tokens: dict[str, dict[str, str]],
    memory_repo: MemoryRepo,
    pack_declared: list[dict[str, Any]],
    frozen_clock: datetime,
) -> None:
    from app.core.config import get_settings

    settings = get_settings()
    run_id = UUID(pack_upload()["run"]["id"])
    page1 = pack_declared[0]["sha256"]
    for variant in ReadVariant:  # page 1 read before this run: extracted and cached
        memory_repo.put_extraction(
            make_extraction(
                page_sha256=page1,
                read_variant=variant,
                model=settings.EXTRACT_MODEL,
                prompt_version=settings.PROMPT_VERSION,
                created_at=frozen_clock - timedelta(days=1),
            )
        )
    memory_repo.add_run_event(
        make_event(
            run_id, stage=Stage.VALIDATE, status=RunEventStatus.OK, at=frozen_clock, duration_ms=7
        )
    )
    _reconciled(memory_repo, run_id)
    resp = client.get(f"{API}/runs/{run_id}", headers=tokens["reviewer"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(run_id) and body["status"] == "UPLOADED"  # flat RunDetail (C25)
    stages = {s["stage"]: s for s in body["stage_progress"]["stages"]}
    assert stages["upload"]["state"] == "OK"
    assert (stages["validate"]["state"], stages["validate"]["duration_ms"]) == ("OK", 7)
    assert stages["extract"]["state"] == "PENDING" and stages["extract"]["at"] is None
    assert body["stage_progress"]["pages"] == [
        {"page_no": 1, "extracted": True, "cached": True},
        {"page_no": 2, "extracted": False, "cached": False},
    ]
    assert body["counts"]["MATCHED"] == 2 and body["proof_status"] == "PASS"
    assert body["open_review_items"] == 1


def test_F01_EP05_unknown_run_404(client: TestClient, tokens: dict[str, dict[str, str]]) -> None:
    resp = client.get(f"{API}/runs/{uuid4()}", headers=tokens["controller"])
    assert resp.status_code == 404
    assert _error(resp)["code"] == "NOT_FOUND"
