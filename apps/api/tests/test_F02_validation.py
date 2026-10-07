"""F-02 input validation over HTTP (004 T034; EP-08, data-model.md §3, contracts/alerts.md).

Broken copies are built from the pack's own bytes in memory (no files on disk); the pack itself
(``pack_upload``) proves the pass path. ``_upload`` mirrors ``test_F01_runs_uploads._create`` /
``_slots`` / ``_confirm`` but lets each test swap in one broken file.
"""

import hashlib
import io
from collections.abc import Callable
from typing import Any
from uuid import UUID

import openpyxl
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.memory_repo import MemoryRepo
from app.core.storage import INPUTS_BUCKET, MemoryBlobStore
from app.domain.enums import AuditAction, ReadVariant, RunEventStatus, RunStatus, Source, Stage
from tests.fakes.api import FakeNotifier

API = "/api/v1"
Headers = dict[str, str]
PackUpload = Callable[..., dict[str, Any]]
BrokenUpload = Callable[[dict[str, bytes]], str]


def _error(resp: Any) -> dict[str, Any]:
    body: dict[str, Any] = resp.json()["error"]
    return body


def _upload(
    client: TestClient,
    headers: Headers,
    memory_blobs: MemoryBlobStore,
    files: dict[str, bytes],
    declared: list[dict[str, Any]],
) -> str:
    """EP-03 -> EP-06 -> put -> EP-07 for the given `files`/`declared`; returns the run id."""
    created = client.post(f"{API}/runs", json={"month": "2026-09"}, headers=headers)
    assert created.status_code == 201, created.text
    run_id: str = created.json()["id"]
    slots = client.post(f"{API}/runs/{run_id}/uploads", json={"files": declared}, headers=headers)
    assert slots.status_code == 200, slots.text
    for slot in slots.json()["uploads"]:
        memory_blobs.put(INPUTS_BUCKET, slot["storage_path"], files[slot["filename"]], "x/y")
    confirmed = client.post(f"{API}/runs/{run_id}/uploads/confirm", headers=headers)
    assert confirmed.status_code == 200, confirmed.text
    return run_id


def _declared(files: dict[str, bytes], sources: dict[str, str]) -> list[dict[str, Any]]:
    return [
        {
            "source": sources[name],
            "filename": name,
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        for name, data in files.items()
    ]


@pytest.fixture
def broken_upload(
    client: TestClient,
    tokens: dict[str, Headers],
    memory_blobs: MemoryBlobStore,
    pack_files: dict[str, bytes],
) -> BrokenUpload:
    """`broken_upload(replacements)`: the pack's files with `replacements` swapped in, uploaded
    and confirmed as the controller; returns the run id."""
    from tests.conftest import PACK_FILES

    def upload(replacements: dict[str, bytes]) -> str:
        files = {**pack_files, **replacements}
        declared = _declared(files, PACK_FILES)
        return _upload(client, tokens["controller"], memory_blobs, files, declared)

    return upload


def _validate(client: TestClient, headers: Headers, run_id: str) -> Any:
    return client.post(f"{API}/runs/{run_id}/validate", headers=headers)


# --- broken file builders (mirrors tests/test_F02_rules.py) -----------------------------

ERP_HEADER = (
    "grn_no,grn_date,supplier_code,supplier_name,item_desc,lot_no,qty,uom,entered_by,entered_at"
)
GOOD_ROW = "GRN-4401,2026-09-01,S-101,QASR,CARTONS,L2609-001,2000,PCS,store.clerk,2026-09-01 18:08"
SHEET_HEADERS = ["Date", "GRN", "Supplier", "Material", "Lot", "Qty Received", "UOM", "Remarks"]


def _erp(*rows: str) -> bytes:
    return "\r\n".join([ERP_HEADER, *rows, ""]).encode()


def _erp_without_lot_no() -> bytes:
    header = ERP_HEADER.replace(",lot_no", "")
    row = GOOD_ROW.replace(",L2609-001", "")
    return "\r\n".join([header, row, ""]).encode()


def _erp_with_bad_qty_on_line(line: int) -> bytes:
    rows = [GOOD_ROW] * 5
    rows[line - 1] = rows[line - 1].replace(",2000,", ",abc,")
    return _erp(*rows)


def _xlsx(title: str = "Sep-2026", headers: list[str] | None = None, rows: int = 1) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = title
    ws.append(headers if headers is not None else SHEET_HEADERS)
    for _ in range(rows):
        ws.append(["9/14/2026", 4401, "Qasr", "Cartons", "L2609-001", 2000, "pcs", None])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


REGISTER_MIN_BYTES = 20 * 1024
"""EP-06's own floor (F-01.R3); padding keeps a deliberately-broken image past it."""


def _png(width: int, height: int) -> bytes:
    import struct
    import zlib

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr
    data = b"\x89PNG\r\n\x1a\n" + chunk + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
    return data.ljust(REGISTER_MIN_BYTES + 1, b"\x00")


def _unreadable_register_bytes() -> bytes:
    return b"not an image".ljust(REGISTER_MIN_BYTES + 1, b"\x00")


# --- AC1 to AC6 (owned acceptance criteria) ----------------------------------------------


def test_F02_AC1_pack_passes(
    pack_upload: PackUpload, client: TestClient, tokens: dict[str, Headers], memory_repo: MemoryRepo
) -> None:
    run_id = pack_upload()["run"]["id"]
    resp = _validate(client, tokens["controller"], run_id)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "problems": []}
    run = client.get(f"{API}/runs/{run_id}", headers=tokens["controller"]).json()
    assert run["status"] == "VALIDATED"
    actions = [e.action for e in memory_repo.list_audit(run_id=UUID(run_id))]
    assert AuditAction.RUN_VALIDATED in actions
    events = [e for e in memory_repo.list_run_events(UUID(run_id)) if e.stage is Stage.VALIDATE]
    assert [e.status for e in events] == [RunEventStatus.OK, RunEventStatus.STARTED]  # newest first


def test_F02_AC2_missing_lot_no_column(
    broken_upload: BrokenUpload, client: TestClient, tokens: dict[str, Headers]
) -> None:
    run_id = broken_upload({"t4_erp_grn.csv": _erp_without_lot_no()})
    resp = _validate(client, tokens["controller"], run_id)
    assert resp.status_code == 422
    err = _error(resp)
    assert err["code"] == "CONTRACT_VIOLATION" and err["stage"] == "validate"
    problems = err["details"]["problems"]
    assert len(problems) == 1
    assert problems[0]["code"] == "MISSING_COLUMN"
    assert "lot_no" in problems[0]["message"]


def test_F02_AC3_renamed_worksheet_lists_found(
    broken_upload: BrokenUpload, client: TestClient, tokens: dict[str, Headers]
) -> None:
    run_id = broken_upload({"t4_store_sheet.xlsx": _xlsx(title="Aug-2026")})
    err = _error(_validate(client, tokens["controller"], run_id))
    problems = err["details"]["problems"]
    assert [p["code"] for p in problems] == ["MISSING_WORKSHEET"]
    assert "Aug-2026" in problems[0]["message"] and "Sep-2026" in problems[0]["message"]


def test_F02_AC4_text_qty_bad_number_with_line(
    broken_upload: BrokenUpload, client: TestClient, tokens: dict[str, Headers]
) -> None:
    run_id = broken_upload({"t4_erp_grn.csv": _erp_with_bad_qty_on_line(5)})
    err = _error(_validate(client, tokens["controller"], run_id))
    problems = err["details"]["problems"]
    assert [p["code"] for p in problems] == ["BAD_NUMBER"]
    assert problems[0]["location"]["line"] == 5


def test_F02_AC5_two_broken_files_both_reported(
    broken_upload: BrokenUpload, client: TestClient, tokens: dict[str, Headers]
) -> None:
    run_id = broken_upload(
        {
            "t4_erp_grn.csv": _erp_without_lot_no(),
            "t4_store_sheet.xlsx": _xlsx(title="Aug-2026"),
        }
    )
    err = _error(_validate(client, tokens["controller"], run_id))
    problems = err["details"]["problems"]
    assert {p["code"] for p in problems} == {"MISSING_COLUMN", "MISSING_WORKSHEET"}
    assert len(problems) == 2


def test_F02_AC6_text_date_not_a_problem(
    pack_upload: PackUpload,
    client: TestClient,
    tokens: dict[str, Headers],
    pack_files: dict[str, bytes],
) -> None:
    """The pack's sheet row 18 date is written as text `9/14/2026`; F-02 does not parse dates."""
    wb = openpyxl.load_workbook(
        io.BytesIO(pack_files["t4_store_sheet.xlsx"]), read_only=True, data_only=True
    )
    try:
        value = wb["Sep-2026"].cell(row=18, column=1).value
    finally:
        wb.close()
    assert value == "9/14/2026"  # as text, not a date; confirms the fixture matches the AC
    run_id = pack_upload()["run"]["id"]
    resp = _validate(client, tokens["controller"], run_id)
    assert resp.status_code == 200
    assert resp.json()["problems"] == []


# --- F-02.R1 to R3 (one representative each at API level; see test_F02_rules.py for units) --


def test_F02_R1_bom_accepted(
    broken_upload: BrokenUpload, client: TestClient, tokens: dict[str, Headers]
) -> None:
    run_id = broken_upload({"t4_erp_grn.csv": b"\xef\xbb\xbf" + _erp(GOOD_ROW)})
    resp = _validate(client, tokens["controller"], run_id)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "problems": []}


def test_F02_R1_bad_iso_date(
    broken_upload: BrokenUpload, client: TestClient, tokens: dict[str, Headers]
) -> None:
    bad = GOOD_ROW.replace("2026-09-01,S", "9/1/2026,S")
    run_id = broken_upload({"t4_erp_grn.csv": _erp(bad)})
    err = _error(_validate(client, tokens["controller"], run_id))
    assert [p["code"] for p in err["details"]["problems"]] == ["BAD_DATE"]


def test_F02_R1_no_data_rows(
    broken_upload: BrokenUpload, client: TestClient, tokens: dict[str, Headers]
) -> None:
    run_id = broken_upload({"t4_erp_grn.csv": _erp()})
    err = _error(_validate(client, tokens["controller"], run_id))
    assert [p["code"] for p in err["details"]["problems"]] == ["NO_DATA_ROWS"]


def test_F02_R1_not_utf8_encoding(
    broken_upload: BrokenUpload, client: TestClient, tokens: dict[str, Headers]
) -> None:
    run_id = broken_upload({"t4_erp_grn.csv": _erp(GOOD_ROW).replace(b"QASR", b"Q\xe9SR")})
    err = _error(_validate(client, tokens["controller"], run_id))
    assert [p["code"] for p in err["details"]["problems"]] == ["ENCODING"]


def test_F02_R2_missing_header(
    broken_upload: BrokenUpload, client: TestClient, tokens: dict[str, Headers]
) -> None:
    headers = [h for h in SHEET_HEADERS if h != "Lot"]
    run_id = broken_upload({"t4_store_sheet.xlsx": _xlsx(headers=headers)})
    err = _error(_validate(client, tokens["controller"], run_id))
    assert [p["code"] for p in err["details"]["problems"]] == ["MISSING_HEADER"]


def test_F02_R3_image_too_small(
    broken_upload: BrokenUpload, client: TestClient, tokens: dict[str, Headers]
) -> None:
    run_id = broken_upload({"t4_register_page1.jpg": _png(799, 1200)})
    err = _error(_validate(client, tokens["controller"], run_id))
    assert [p["code"] for p in err["details"]["problems"]] == ["IMAGE_TOO_SMALL"]


def test_F02_R3_image_unreadable(
    broken_upload: BrokenUpload, client: TestClient, tokens: dict[str, Headers]
) -> None:
    run_id = broken_upload({"t4_register_page1.jpg": _unreadable_register_bytes()})
    err = _error(_validate(client, tokens["controller"], run_id))
    assert [p["code"] for p in err["details"]["problems"]] == ["IMAGE_UNREADABLE"]


# --- F-02.R5: failure effects -------------------------------------------------------------


def _ops_chat(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("TELEGRAM_OPS_CHAT_ID", "ops-chat")
    get_settings.cache_clear()
    return "ops-chat"


def test_F02_R5_failure_sets_failed_validate_and_one_ops_alert(
    broken_upload: BrokenUpload,
    client: TestClient,
    tokens: dict[str, Headers],
    memory_repo: MemoryRepo,
    notifier: FakeNotifier,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat_id_expected = _ops_chat(monkeypatch)
    run_id = broken_upload({"t4_erp_grn.csv": _erp_without_lot_no()})
    resp = _validate(client, tokens["controller"], run_id)
    assert resp.status_code == 422
    run = memory_repo.get_run(UUID(run_id))
    assert run.status is RunStatus.FAILED and run.failed_stage is Stage.VALIDATE
    # item 3: error_message is the exact capped summary, not a free-form string
    assert run.error_message == "1 problem: ERP: column lot_no is missing"
    assert len(notifier.sent) == 1
    chat_id, text = notifier.sent[0]
    assert chat_id == chat_id_expected == get_settings().TELEGRAM_OPS_CHAT_ID
    assert "Controller was NOT messaged" in text
    assert "Stage: validate" in text
    actions = [e.action for e in memory_repo.list_audit(run_id=UUID(run_id))]
    assert AuditAction.ALERT_SENT in actions
    assert AuditAction.RUN_VALIDATED not in actions
    events = [e for e in memory_repo.list_run_events(UUID(run_id)) if e.stage is Stage.VALIDATE]
    assert [e.status for e in events] == [RunEventStatus.FAILED, RunEventStatus.STARTED]


def test_F02_R5_no_data_changed(
    broken_upload: BrokenUpload,
    client: TestClient,
    tokens: dict[str, Headers],
    memory_repo: MemoryRepo,
    memory_blobs: MemoryBlobStore,
) -> None:
    """Item 8: a failing validate leaves the source rows, the stored objects and any
    extraction rows exactly as confirm left them."""
    run_id = broken_upload({"t4_erp_grn.csv": _erp_without_lot_no()})
    before = memory_repo.list_source_files(UUID(run_id))
    before_blobs = {f.storage_path: memory_blobs.get(INPUTS_BUCKET, f.storage_path) for f in before}
    resp = _validate(client, tokens["controller"], run_id)
    assert resp.status_code == 422
    after = memory_repo.list_source_files(UUID(run_id))
    assert after == before
    assert {f.storage_path: memory_blobs.get(INPUTS_BUCKET, f.storage_path) for f in after} == (
        before_blobs
    )
    settings = get_settings()
    for f in before:
        if f.source is Source.REGISTER:
            for variant in ReadVariant:
                assert (
                    memory_repo.get_extraction(
                        f.sha256, variant, settings.EXTRACT_MODEL, settings.PROMPT_VERSION
                    )
                    is None
                )


def test_F02_R5_alert_reason_omits_file_contents_shows_codes_and_locations(
    broken_upload: BrokenUpload,
    client: TestClient,
    tokens: dict[str, Headers],
    notifier: FakeNotifier,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 2: the ops alert's reason names codes and locations only, never a cell's own text
    (here the ERP's actual bad `qty` value, "abc", which the human-facing message does quote)."""
    _ops_chat(monkeypatch)
    run_id = broken_upload({"t4_erp_grn.csv": _erp_with_bad_qty_on_line(5)})
    resp = _validate(client, tokens["controller"], run_id)
    assert resp.status_code == 422
    assert "abc" in _error(resp)["details"]["problems"][0]["message"]  # the 422 body is unchanged
    assert len(notifier.sent) == 1
    _, text = notifier.sent[0]
    assert "abc" not in text
    assert "Error: CONTRACT_VIOLATION: 1 problem (ERP line 5 qty BAD_NUMBER)" in text


def test_F02_R5_alert_reason_caps_at_three_with_and_n_more(
    broken_upload: BrokenUpload,
    client: TestClient,
    tokens: dict[str, Headers],
    notifier: FakeNotifier,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ops_chat(monkeypatch)
    headers = [h for h in SHEET_HEADERS if h not in ("Lot", "UOM", "Date", "Remarks")]
    run_id = broken_upload(
        {
            "t4_erp_grn.csv": _erp_without_lot_no(),
            "t4_store_sheet.xlsx": _xlsx(headers=headers),
        }
    )
    resp = _validate(client, tokens["controller"], run_id)
    problems = _error(resp)["details"]["problems"]
    assert len(problems) == 5  # 1 ERP + 4 MISSING_HEADER
    _, text = notifier.sent[0]
    assert "5 problems (" in text
    assert "and 2 more)" in text


def test_F02_R5_no_model_calls_on_failure(
    broken_upload: BrokenUpload,
    client: TestClient,
    tokens: dict[str, Headers],
    memory_repo: MemoryRepo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 1: the real extraction entry point is monkeypatched so a future `validate_stage`
    that wrongly calls it would fail this test; plus no EXTRACT event or extraction row."""
    from app.pipeline import extract as extract_module
    from tests.fakes.api import FakeExtractor

    extractor = FakeExtractor()
    monkeypatch.setattr(extract_module, "extract_page", extractor)
    run_id = broken_upload({"t4_erp_grn.csv": _erp_without_lot_no()})
    resp = _validate(client, tokens["controller"], run_id)
    assert resp.status_code == 422
    assert extractor.calls == []
    events = memory_repo.list_run_events(UUID(run_id))
    assert [e for e in events if e.stage is Stage.EXTRACT] == []
    settings = get_settings()
    for f in memory_repo.list_source_files(UUID(run_id), Source.REGISTER):
        for variant in ReadVariant:
            assert (
                memory_repo.get_extraction(
                    f.sha256, variant, settings.EXTRACT_MODEL, settings.PROMPT_VERSION
                )
                is None
            )


def test_F02_missing_input_blob_500_stays_uploaded_one_alert(
    broken_upload: BrokenUpload,
    client: TestClient,
    tokens: dict[str, Headers],
    memory_repo: MemoryRepo,
    memory_blobs: MemoryBlobStore,
    notifier: FakeNotifier,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 4: an input object missing from storage after confirm (the API's own fault, not a
    contract problem) is a 500, not a 404; the run stays UPLOADED so EP-08 can be retried once
    the object is restored, and exactly one ops alert is sent."""
    _ops_chat(monkeypatch)
    run_id = broken_upload({})  # the unmodified pack
    erp = next(f for f in memory_repo.list_source_files(UUID(run_id)) if f.source is Source.ERP)
    memory_blobs.delete(INPUTS_BUCKET, erp.storage_path)
    resp = _validate(client, tokens["controller"], run_id)
    assert resp.status_code == 500
    assert _error(resp)["code"] == "INTERNAL_ERROR"
    run = memory_repo.get_run(UUID(run_id))
    assert run.status is RunStatus.UPLOADED and run.failed_stage is None
    events = [e for e in memory_repo.list_run_events(UUID(run_id)) if e.stage is Stage.VALIDATE]
    assert [e.status for e in events] == [RunEventStatus.FAILED, RunEventStatus.STARTED]
    assert len(notifier.sent) == 1


# --- idempotency and retry rules (data-model.md §3) ---------------------------------------


def test_F02_validate_twice_idempotent(
    pack_upload: PackUpload, client: TestClient, tokens: dict[str, Headers], memory_repo: MemoryRepo
) -> None:
    run_id = pack_upload()["run"]["id"]
    first = _validate(client, tokens["controller"], run_id)
    assert first.status_code == 200
    audit_count = len(memory_repo.list_audit(run_id=UUID(run_id)))
    events_count = len(memory_repo.list_run_events(UUID(run_id)))
    second = _validate(client, tokens["controller"], run_id)
    assert second.status_code == 200
    assert second.json() == {"ok": True, "problems": []}
    assert len(memory_repo.list_audit(run_id=UUID(run_id))) == audit_count
    assert len(memory_repo.list_run_events(UUID(run_id))) == events_count


def test_F02_validate_on_failed_validate_409(
    broken_upload: BrokenUpload, client: TestClient, tokens: dict[str, Headers]
) -> None:
    run_id = broken_upload({"t4_erp_grn.csv": _erp_without_lot_no()})
    first = _validate(client, tokens["controller"], run_id)
    assert first.status_code == 422
    retry = _validate(client, tokens["controller"], run_id)
    assert retry.status_code == 409
    assert _error(retry)["code"] == "RUN_STATE_CONFLICT"
    # OPS "retry" is also refused: validate has no retry from FAILED (FR-024, new run instead)
    ops_retry = _validate(client, tokens["ops"], run_id)
    assert ops_retry.status_code == 409
    assert _error(ops_retry)["code"] == "RUN_STATE_CONFLICT"


def test_F02_ops_on_run_failed_at_extract_409(
    pack_upload: PackUpload, client: TestClient, tokens: dict[str, Headers], memory_repo: MemoryRepo
) -> None:
    """The `ops_retry` guard admits OPS whenever the run is `FAILED`, whatever the stage; EP-08
    itself still only accepts `UPLOADED` (data-model.md §3), so this is 409, not a retry."""
    run_id = UUID(pack_upload()["run"]["id"])
    memory_repo.transition_run(run_id, RunStatus.VALIDATED, expected={RunStatus.UPLOADED})
    memory_repo.transition_run(run_id, RunStatus.EXTRACTING, expected={RunStatus.VALIDATED})
    memory_repo.transition_run(
        run_id,
        RunStatus.FAILED,
        expected={RunStatus.EXTRACTING},
        failed_stage=Stage.EXTRACT,
        error_message="x",
    )
    resp = _validate(client, tokens["ops"], str(run_id))
    assert resp.status_code == 409
    assert _error(resp)["code"] == "RUN_STATE_CONFLICT"


def test_F02_ops_on_uploaded_run_403(client: TestClient, tokens: dict[str, Headers]) -> None:
    """OPS may only call EP-08 to retry a `FAILED` run (contracts/auth.md); on an ordinary
    `UPLOADED` run the role guard itself refuses before the service is reached."""
    created = client.post(f"{API}/runs", json={"month": "2026-09"}, headers=tokens["controller"])
    run_id = created.json()["id"]
    resp = _validate(client, tokens["ops"], run_id)
    assert resp.status_code == 403
    assert _error(resp)["code"] == "NOT_ALLOWED"


def test_F02_validate_concurrent_repeat_no_failed_event_left_behind(
    pack_upload: PackUpload,
    memory_repo: MemoryRepo,
    memory_blobs: MemoryBlobStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 5: a losing concurrent validate call (the winner already moved the run past
    `UPLOADED`) answers `{ok: true}` with no `FAILED` run event — mirrors
    `test_svc_confirm_lost_race_answers_as_repeat_without_audit`."""
    from app.core.errors import StateConflict
    from app.domain.enums import Role
    from app.services.stages import validate_stage
    from tests.factories import make_user

    run_id = UUID(pack_upload()["run"]["id"])
    run = memory_repo.get_run(run_id)
    real = memory_repo.transition_run

    def racing(run_id_: Any, to: RunStatus, **kw: Any) -> Any:
        current = real(run_id_, to, **kw)  # another validate call wins the race
        raise StateConflict(current)

    monkeypatch.setattr(memory_repo, "transition_run", racing)
    user = make_user(role=Role.CONTROLLER)
    problems = validate_stage(memory_repo, memory_blobs, FakeNotifier(), run, user)
    assert problems == []
    events = [e for e in memory_repo.list_run_events(run_id) if e.stage is Stage.VALIDATE]
    assert RunEventStatus.FAILED not in {e.status for e in events}
    assert memory_repo.get_run(run_id).status is RunStatus.VALIDATED
