"""F-10 finalise and the Telegram summary: EP-17 (005 US4, T049 to T056; spec FR-025 to FR-036,
FM-10 to FM-12, contracts/telegram-message.md, data-model.md §4, §6, §8).

Every test starts from `pack_run`: the pack reconciled to `AWAITING_REVIEW` (21/4/2/2, proof
PASS), clock frozen at the scenario instant; `fake_notifier` is the app's notifier.
"""

import re
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from app.core.config import get_settings
from app.core.storage import REPORTS_BUCKET, MemoryBlobStore
from app.domain.enums import (
    AuditAction,
    NotificationKind,
    NotificationStatus,
    RunEventStatus,
    RunStatus,
    Stage,
)
from app.domain.ids import summary_key
from app.domain.models import ExceptionItem, Notification, Proof
from app.pipeline import report
from app.services import telegram
from app.services.finalise import load_run_view, report_paths
from tests.factories import make_run, mapping_review
from tests.fakes.api import FakeNotifier
from tests.support.pack_run import PackRun
from tests.support.trace import assert_traced

API = "/api/v1"
Client = Callable[[str], TestClient]
CONTROLLER_CHAT = "controller-chat"
OPS_CHAT = "ops-chat"
CONFIRM = {"confirm": True}

GOLDEN = """Sep 2026 receiving: 29 GRNs, 21 match, 8 need attention

ACT ON THESE
1. 4421 stretch film, 75 ROLL: in register + sheet, NOT in ERP
2. 4430 cartons 5-ply, 1900 PCS: ERP only, dated 29 Sep, keyed 2 Oct by admin2

SOURCES DISAGREE
3. 4417 qty: ERP 46 CTN, register + sheet 48 CTN
4. 4425 qty: sheet 1200 M, register 1200 YD = ERP 1097.28 M
5. 4412 lot: ERP L2609-021, register + sheet L2609-012
6. 4415 date: ERP 13 Sep (Sun), register + sheet 12 Sep

WAITING ON REVIEW
7. 4428 qty unreadable on register (ink blot)
8. 4419 qty corrected on register, 60 to 66

Also: GRN 4429 missing everywhere. Sheet lists 4409 twice and one return (-5000 PCS).
Proof: 86 of 86 rows placed once.
Report: {APP_BASE_URL}/runs/{id}"""

GOLDEN_AFTER_REVIEW = """Sep 2026 receiving: 29 GRNs, 22 match, 7 need attention

ACT ON THESE
1. 4421 stretch film, 75 ROLL: in register + sheet, NOT in ERP
2. 4430 cartons 5-ply, 1900 PCS: ERP only, dated 29 Sep, keyed 2 Oct by admin2

SOURCES DISAGREE
3. 4417 qty: ERP 46 CTN, register + sheet 48 CTN
4. 4419 qty: sheet 60 DRUM, register + ERP 66 DRUM
5. 4425 qty: sheet 1200 M, register 1200 YD = ERP 1097.28 M
6. 4412 lot: ERP L2609-021, register + sheet L2609-012
7. 4415 date: ERP 13 Sep (Sun), register + sheet 12 Sep

Also: GRN 4429 missing everywhere. Sheet lists 4409 twice and one return (-5000 PCS).
Proof: 86 of 86 rows placed once.
Report: {APP_BASE_URL}/runs/{id}"""

REVIEW_DECISIONS = (
    ("CELL:REG-p2-r05:qty", {"action": "ACCEPT_READ"}),
    ("CELL:REG-p2-r14:qty", {"action": "ENTER_VALUE", "value": "250"}),
    ("FLAG:SEQUENCE_GAP:4429", {"action": "ACKNOWLEDGE"}),
    ("FLAG:BACKDATED_ENTRY:ERP-0028", {"action": "ACKNOWLEDGE"}),
)
"""Contracts/telegram-message.md second golden: 4419 ACCEPT_READ, 4428 ENTER_VALUE 250, 4429 and
4430 flags ACKNOWLEDGE."""


@pytest.fixture(autouse=True)
def chats(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CONTROLLER_CHAT_ID", CONTROLLER_CHAT)
    monkeypatch.setenv("TELEGRAM_OPS_CHAT_ID", OPS_CHAT)
    get_settings.cache_clear()


# --- helpers ---------------------------------------------------------------------------


def _finalise(
    client: TestClient, pack: PackRun, body: Any = CONFIRM, fault: str | None = None
) -> Response:
    params = {"fault": fault} if fault else None
    return client.post(f"{API}/runs/{pack.run_id}/finalise", json=body, params=params)


def _ok(resp: Response) -> dict[str, Any]:
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


def _error(resp: Response, status: int, code: str) -> dict[str, Any]:
    assert resp.status_code == status, resp.text
    error: dict[str, Any] = resp.json()["error"]
    assert error["code"] == code, error
    return error


def _golden(text: str, pack: PackRun) -> str:
    base = get_settings().APP_BASE_URL.rstrip("/")
    return text.replace("{APP_BASE_URL}", base).replace("{id}", str(pack.run_id))


def _proof(pack: PackRun) -> Proof:
    proof = pack.repo.get_proof(pack.run_id)
    assert proof is not None
    return proof


def _message(pack: PackRun) -> str:
    view = load_run_view(pack.repo, pack.run_id)
    return telegram.build_summary_message(view, get_settings().APP_BASE_URL, _proof(pack)).render()


def _summaries(pack: PackRun) -> list[Notification]:
    return [
        n for n in pack.repo.list_notifications(pack.run_id) if n.kind is NotificationKind.SUMMARY
    ]


def _alerts(pack: PackRun) -> list[Notification]:
    return [
        n for n in pack.repo.list_notifications(pack.run_id) if n.kind is NotificationKind.ALERT
    ]


def _events(pack: PackRun, stage: Stage) -> list[RunEventStatus]:
    """Statuses of `stage`'s run events, oldest first."""
    return [e.status for e in reversed(pack.repo.list_run_events(pack.run_id)) if e.stage is stage]


def _audits(pack: PackRun, action: AuditAction) -> list[Any]:
    return [a for a in pack.repo.list_audit(run_id=pack.run_id) if a.action is action]


def _put_summary(pack: PackRun, status: NotificationStatus, updated_at: datetime) -> Notification:
    """A SUMMARY row as a crashed or concurrent send leaves it; the run set FINALISED first."""
    run = pack.repo.get_run(pack.run_id)
    assert run.run_key is not None
    pack.repo.transition_run(
        run.id,
        RunStatus.FINALISED,
        expected={run.status},
        finalised_by="controller@kestrel.example",
        finalised_at=updated_at,
    )
    n, created = pack.repo.insert_notification(
        Notification(
            id=uuid4(),
            run_id=run.id,
            kind=NotificationKind.SUMMARY,
            idempotency_key=summary_key(run.run_key),
            status=status,
            attempts=1,
            last_error=None,
            telegram_message_id=None,
            sent_at=None,
            updated_at=updated_at,
        )
    )
    assert created
    return n


def _review_all(api_client_as: Client, pack: PackRun) -> None:
    client = api_client_as("controller")
    items = {i.item_key: i.id for i in pack.repo.list_review_items(pack.run_id)}
    for key, body in REVIEW_DECISIONS:
        resp = client.post(f"{API}/review-items/{items[key]}/resolve", json=body)
        assert resp.status_code == 200, resp.text


# --- T049 to T054: acceptance ----------------------------------------------------------


def test_F10_AC1_one_message_one_document(
    pack_run: PackRun, api_client_as: Client, fake_notifier: FakeNotifier
) -> None:
    body = _ok(_finalise(api_client_as("controller"), pack_run))
    assert len(fake_notifier.texts(CONTROLLER_CHAT)) == 1
    docs = fake_notifier.documents_to(CONTROLLER_CHAT)
    assert [name for name, _ in docs] == ["report.html"]
    html_path, _ = report_paths(pack_run.run_id)
    assert docs[0][1] == pack_run.blobs.get(REPORTS_BUCKET, html_path)
    (summary,) = _summaries(pack_run)
    assert summary.status is NotificationStatus.SENT
    assert summary.telegram_message_id is not None and summary.sent_at is not None
    assert body["notification"]["id"] == str(summary.id)
    assert body["notification"]["status"] == "SENT"
    assert body["run"]["status"] == "FINALISED"
    assert body["run"]["summary"]["status"] == "SENT"
    assert body["report_url"]
    (sent,) = _audits(pack_run, AuditAction.SUMMARY_SENT)
    assert sent.channel.value == "TELEGRAM"
    assert sent.after["telegram_message_id"] == summary.telegram_message_id


def test_F10_AC2_second_finalise_sends_nothing_same_notification(
    pack_run: PackRun, api_client_as: Client, fake_notifier: FakeNotifier
) -> None:
    client = api_client_as("controller")
    first = _ok(_finalise(client, pack_run))
    second = _ok(_finalise(client, pack_run))
    assert second["notification"] == first["notification"]
    assert len(fake_notifier.sent) == 1 and len(fake_notifier.documents) == 1
    assert len(_summaries(pack_run)) == 1
    assert len(_audits(pack_run, AuditAction.RUN_FINALISED)) == 1


def test_F10_AC3_telegram_down_then_resend_exactly_one(
    faults_on: None, pack_run: PackRun, api_client_as: Client, fake_notifier: FakeNotifier
) -> None:
    client = api_client_as("controller")
    down = _ok(_finalise(client, pack_run, fault="TELEGRAM_DOWN"))
    assert down["run"]["status"] == "FINALISED"
    assert down["notification"]["status"] == "FAILED"
    assert down["notification"]["last_error"]
    assert pack_run.repo.get_run(pack_run.run_id).status is RunStatus.FINALISED
    assert len(_alerts(pack_run)) == 1
    assert len(_audits(pack_run, AuditAction.SUMMARY_FAILED)) == 1
    assert fake_notifier.sent == [] and fake_notifier.documents == []

    resent = _ok(_finalise(client, pack_run))
    assert resent["notification"]["id"] == down["notification"]["id"]
    assert resent["notification"]["status"] == "SENT"
    assert resent["notification"]["attempts"] == 2
    assert len(fake_notifier.texts(CONTROLLER_CHAT)) == 1
    assert len(fake_notifier.documents_to(CONTROLLER_CHAT)) == 1
    _ok(_finalise(client, pack_run))
    assert len(fake_notifier.texts(CONTROLLER_CHAT)) == 1
    assert len(_audits(pack_run, AuditAction.RUN_FINALISED)) == 1


def test_F10_AC4_proof_failing_nothing_sent_not_finalised(
    faults_on: None, pack_run: PackRun, api_client_as: Client, fake_notifier: FakeNotifier
) -> None:
    client = api_client_as("controller")
    reconciled = client.post(
        f"{API}/runs/{pack_run.run_id}/reconcile", params={"fault": "PROOF_DROP_ROW"}
    )
    assert reconciled.status_code == 200, reconciled.text
    before = pack_run.repo.get_run(pack_run.run_id)
    _error(_finalise(client, pack_run), 409, "PROOF_NOT_PASSED")
    assert pack_run.repo.get_run(pack_run.run_id) == before
    assert before.status is not RunStatus.FINALISED
    assert fake_notifier.texts(CONTROLLER_CHAT) == []
    assert fake_notifier.documents_to(CONTROLLER_CHAT) == []
    assert _summaries(pack_run) == []
    for path in report_paths(pack_run.run_id):
        assert not pack_run.blobs.exists(REPORTS_BUCKET, path)


def test_F10_AC5_every_number_in_message_in_data(
    pack_run: PackRun, api_client_as: Client, fake_notifier: FakeNotifier
) -> None:
    _ok(_finalise(api_client_as("controller"), pack_run))
    (text,) = fake_notifier.texts(CONTROLLER_CHAT)
    assert_traced(text, pack_run.repo, pack_run.run_id)


def test_F10_AC6_message_follows_exception_order(pack_run: PackRun) -> None:
    view = load_run_view(pack_run.repo, pack_run.run_id)
    message = telegram.build_summary_message(view, "https://x.example", _proof(pack_run))
    numbered = [(n, t) for s in message.sections for n, t in s.items]
    expected = [e.text for e in view.exceptions if e.category <= 3][:8]
    assert [t for _, t in numbered] == expected
    assert [n for n, _ in numbered] == list(range(1, len(expected) + 1))
    titles = {1: "ACT ON THESE", 2: "SOURCES DISAGREE", 3: "WAITING ON REVIEW"}
    cats = [e.category for e in view.exceptions if e.category <= 3][:8]
    assert [s.title for s in message.sections] == [titles[c] for c in dict.fromkeys(cats)]


# --- T055: golden and format -----------------------------------------------------------


def test_F10_golden_pack_message(pack_run: PackRun) -> None:
    assert _message(pack_run) == _golden(GOLDEN, pack_run)


def test_F10_golden_pack_message_after_review(pack_run: PackRun, api_client_as: Client) -> None:
    _review_all(api_client_as, pack_run)
    assert _message(pack_run) == _golden(GOLDEN_AFTER_REVIEW, pack_run)


def _synthetic(n: int) -> list[ExceptionItem]:
    cats = [1, 1, 2, 2, 2, 3, 3, 3, 3, 3, 3][:n]
    kinds = {1: "MISSING", 2: "MISMATCH", 3: "REVIEW"}
    return [
        ExceptionItem(
            category=c,
            kind=kinds[c],
            grn_no=4400 + i,
            group_key=f"GRN:{4400 + i}",
            source_row_id=None,
            field=None,
            flags=[],
            text=f"{4400 + i} item {c}",
        )
        for i, c in enumerate(cats)
    ]


def test_F10_R8_limits_more_and_also(pack_run: PackRun) -> None:
    view = load_run_view(pack_run.repo, pack_run.run_id)
    also = [e for e in view.exceptions if e.category >= 4]
    view = view.model_copy(update={"exceptions": _synthetic(11) + also})
    message = telegram.build_summary_message(view, "https://x.example", _proof(pack_run))
    text = message.render()
    lines = text.split("\n")
    assert sum(len(s.items) for s in message.sections) == 8
    assert message.more == 3
    assert "+3 more" in lines
    assert lines.index("+3 more") < lines.index(message.also or "")
    assert lines[-2:] == [
        "Proof: 86 of 86 rows placed once.",
        f"Report: https://x.example/runs/{pack_run.run_id}",
    ]
    assert len(lines) <= 20 and len(text) < 4096
    assert lines[-3].startswith("Also: ")


def test_F10_R8_no_exceptions_line(pack_run: PackRun) -> None:
    view = load_run_view(pack_run.repo, pack_run.run_id)
    view = view.model_copy(update={"exceptions": []})
    text = telegram.build_summary_message(view, "https://x.example/", _proof(pack_run)).render()
    assert text.split("\n") == [
        "Sep 2026 receiving: 29 GRNs, 21 match, 8 need attention",
        "",
        "No exceptions.",
        "",
        "Proof: 86 of 86 rows placed once.",
        f"Report: https://x.example/runs/{pack_run.run_id}",
    ]


def test_F10_R8_render_rejects_too_long() -> None:
    long = telegram.SummaryMessage(
        headline="h",
        sections=[telegram.MessageSection(title="ACT ON THESE", items=[(1, "x" * 4100)])],
        more=0,
        also=None,
        proof_line="Proof: 1 of 1 rows placed once.",
        link="Report: l",
    )
    with pytest.raises(telegram.MessageTooLong):
        long.render()


# --- T056: requirements ----------------------------------------------------------------


def test_F10_R7_stale_sending_alerts_once_never_resends(
    pack_run: PackRun, api_client_as: Client, fake_notifier: FakeNotifier, frozen_clock: datetime
) -> None:
    stale = _put_summary(pack_run, NotificationStatus.SENDING, frozen_clock - timedelta(minutes=3))
    client = api_client_as("controller")
    for _ in range(2):
        error = _error(_finalise(client, pack_run), 409, "RUN_STATE_CONFLICT")
        assert "by hand" in error["message"]
    assert _ok(client.get(f"{API}/runs/{pack_run.run_id}"))["summary"]["status"] == "SENDING"
    assert fake_notifier.texts(CONTROLLER_CHAT) == []
    assert fake_notifier.documents == []
    assert len(fake_notifier.texts(OPS_CHAT)) == 1
    (alert,) = _alerts(pack_run)
    assert alert.idempotency_key == f"ALERT:{pack_run.run_id}:notify:stale"
    assert pack_run.repo.get_notification_by_key(stale.idempotency_key) == stale


def test_F10_R7_check_stale_summary_unit(
    pack_run: PackRun, frozen_clock: datetime, fake_notifier: FakeNotifier
) -> None:
    repo = pack_run.repo
    assert telegram.check_stale_summary(repo, fake_notifier, pack_run.run_id) is None
    fresh = _put_summary(pack_run, NotificationStatus.SENDING, frozen_clock - timedelta(minutes=2))
    assert telegram.check_stale_summary(repo, fake_notifier, pack_run.run_id) is None
    assert fake_notifier.sent == []
    old = frozen_clock - timedelta(minutes=2, seconds=1)
    repo.update_notification(fresh.id, expected={NotificationStatus.SENDING}, updated_at=old)
    found = telegram.check_stale_summary(repo, fake_notifier, pack_run.run_id)
    assert found is not None and found.id == fresh.id
    assert telegram.check_stale_summary(repo, fake_notifier, pack_run.run_id) is not None
    assert len(fake_notifier.texts(OPS_CHAT)) == 1
    assert fake_notifier.texts(CONTROLLER_CHAT) == []


def test_F10_R6_message_sent_document_failed_resend_document_only(
    pack_run: PackRun, api_client_as: Client, fake_notifier: FakeNotifier
) -> None:
    client = api_client_as("controller")
    fake_notifier.fail_document = True
    failed = _ok(_finalise(client, pack_run))["notification"]
    assert failed["status"] == "FAILED"
    assert failed["telegram_message_id"] is not None
    assert len(fake_notifier.texts(CONTROLLER_CHAT)) == 1
    fake_notifier.fail_document = False
    sent = _ok(_finalise(client, pack_run))["notification"]
    assert sent["status"] == "SENT"
    assert sent["telegram_message_id"] == failed["telegram_message_id"]
    assert len(fake_notifier.texts(CONTROLLER_CHAT)) == 1
    assert len(fake_notifier.documents_to(CONTROLLER_CHAT)) == 1


def test_F10_FM10_report_failure_leaves_run_unchanged(
    pack_run: PackRun,
    api_client_as: Client,
    fake_notifier: FakeNotifier,
    memory_blobs: MemoryBlobStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_put(bucket: str, path: str, data: bytes, content_type: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(memory_blobs, "put", broken_put)
    before = pack_run.repo.get_run(pack_run.run_id)
    resp = _finalise(api_client_as("controller"), pack_run)
    assert resp.status_code == 500, resp.text
    assert resp.json()["error"]["stage"] == "finalise"
    assert pack_run.repo.get_run(pack_run.run_id) == before
    assert fake_notifier.texts(CONTROLLER_CHAT) == [] and fake_notifier.documents == []
    assert _summaries(pack_run) == []
    assert len(fake_notifier.texts(OPS_CHAT)) == 1
    assert _events(pack_run, Stage.FINALISE) == [RunEventStatus.STARTED, RunEventStatus.FAILED]
    assert _audits(pack_run, AuditAction.RUN_FINALISED) == []


@pytest.mark.parametrize("role", ["reviewer", "ops"])
def test_F10_R1_roles_reviewer_ops_403(
    role: str, pack_run: PackRun, api_client_as: Client, fake_notifier: FakeNotifier
) -> None:
    before = pack_run.repo.get_run(pack_run.run_id)
    _error(_finalise(api_client_as(role), pack_run), 403, "NOT_ALLOWED")
    assert pack_run.repo.get_run(pack_run.run_id) == before
    assert fake_notifier.sent == [] and fake_notifier.documents == []


@pytest.mark.parametrize("body", [None, {}, {"confirm": False}])
def test_F10_R1_confirm_required_400(
    body: Any, pack_run: PackRun, api_client_as: Client, fake_notifier: FakeNotifier
) -> None:
    before = pack_run.repo.get_run(pack_run.run_id)
    _error(_finalise(api_client_as("controller"), pack_run, body), 400, "CONFIRM_REQUIRED")
    assert pack_run.repo.get_run(pack_run.run_id) == before
    assert fake_notifier.sent == [] and _summaries(pack_run) == []


def test_F10_R4_sending_fresh_returns_409(
    pack_run: PackRun, api_client_as: Client, fake_notifier: FakeNotifier, frozen_clock: datetime
) -> None:
    _put_summary(pack_run, NotificationStatus.SENDING, frozen_clock)
    error = _error(_finalise(api_client_as("controller"), pack_run), 409, "RUN_STATE_CONFLICT")
    assert "in progress" in error["message"]
    assert fake_notifier.sent == [] and fake_notifier.documents == []
    assert _alerts(pack_run) == []


def test_F10_R5_retries_three_attempts_then_failed(
    faults_on: None, pack_run: PackRun, api_client_as: Client
) -> None:
    body = _ok(_finalise(api_client_as("controller"), pack_run, fault="TELEGRAM_DOWN"))
    assert body["notification"]["status"] == "FAILED"
    assert body["notification"]["attempts"] == 1
    assert _events(pack_run, Stage.NOTIFY) == [
        RunEventStatus.STARTED,
        RunEventStatus.RETRY,
        RunEventStatus.RETRY,
        RunEventStatus.FAILED,
    ]
    retries = [
        e
        for e in pack_run.repo.list_run_events(pack_run.run_id)
        if e.stage is Stage.NOTIFY and e.status is RunEventStatus.RETRY
    ]
    assert sorted(e.detail["attempt"] for e in retries) == [2, 3]
    (failed,) = _audits(pack_run, AuditAction.SUMMARY_FAILED)
    assert failed.after["status"] == "FAILED" and failed.before == {"status": "SENDING"}


def test_F10_R3_finalised_at_equals_report_time(
    pack_run: PackRun, api_client_as: Client, frozen_clock: datetime
) -> None:
    _ok(_finalise(api_client_as("controller"), pack_run))
    run = pack_run.repo.get_run(pack_run.run_id)
    assert run.finalised_at == frozen_clock
    assert run.finalised_by == "controller@kestrel.example"
    html_path, _ = report_paths(pack_run.run_id)
    html = pack_run.blobs.get(REPORTS_BUCKET, html_path).decode()
    assert report.fmt_instant(run.finalised_at) in html
    (audit,) = _audits(pack_run, AuditAction.RUN_FINALISED)
    assert audit.before == {"status": "AWAITING_REVIEW", "open_review_items": 4}
    assert audit.after["status"] == "FINALISED"
    assert audit.after["finalised_at"] == run.finalised_at.isoformat()
    assert audit.after["report_paths"] == list(report_paths(pack_run.run_id))


def test_F10_R2_run_events_finalise_and_notify(pack_run: PackRun, api_client_as: Client) -> None:
    _ok(_finalise(api_client_as("controller"), pack_run))
    assert _events(pack_run, Stage.FINALISE) == [RunEventStatus.STARTED, RunEventStatus.OK]
    assert _events(pack_run, Stage.NOTIFY) == [RunEventStatus.STARTED, RunEventStatus.OK]


def test_F10_run_view_summary_ignores_other_runs_notification(
    pack_run: PackRun, frozen_clock: datetime
) -> None:
    """C7: two unfinished runs can share a run key; a SUMMARY row of another run is not this
    run's summary."""
    run = pack_run.repo.get_run(pack_run.run_id)
    assert run.run_key is not None
    other = pack_run.repo.create_run(make_run(run_key=run.run_key)).id
    pack_run.repo.insert_notification(
        Notification(
            id=uuid4(),
            run_id=other,
            kind=NotificationKind.SUMMARY,
            idempotency_key=summary_key(run.run_key),
            status=NotificationStatus.SENT,
            attempts=1,
            last_error=None,
            telegram_message_id=7,
            sent_at=frozen_clock,
            updated_at=frozen_clock,
        )
    )
    assert load_run_view(pack_run.repo, pack_run.run_id).summary is None


def _printed_finalised_at(pack: PackRun) -> str:
    html_path, _ = report_paths(pack.run_id)
    html = pack.blobs.get(REPORTS_BUCKET, html_path).decode()
    found = re.search(r"<dt>Finalised at</dt><dd>([^<]*) \(Asia/Karachi\)</dd>", html)
    assert found is not None
    return found.group(1)


def test_F10_R3_printed_finalised_at_equals_run(pack_run: PackRun, api_client_as: Client) -> None:
    _ok(_finalise(api_client_as("controller"), pack_run))
    run = pack_run.repo.get_run(pack_run.run_id)
    assert _printed_finalised_at(pack_run) == report.fmt_instant(run.finalised_at)


def test_F10_FM10_crash_after_csv_retry_prints_its_own_instant(
    pack_run: PackRun,
    api_client_as: Client,
    fake_notifier: FakeNotifier,
    memory_blobs: MemoryBlobStore,
    monkeypatch: pytest.MonkeyPatch,
    move_clock: Callable[[datetime], None],
    frozen_clock: datetime,
) -> None:
    """research R9: the CSV is written first and the timestamped report.html last, so a crash
    between them leaves report.html missing and the retry prints the instant it finalises at."""
    put = memory_blobs.put

    def html_fails(bucket: str, path: str, data: bytes, content_type: str) -> None:
        if path.endswith("report.html"):
            raise OSError("crash")
        put(bucket, path, data, content_type)

    monkeypatch.setattr(memory_blobs, "put", html_fails)
    client = api_client_as("controller")
    assert _finalise(client, pack_run).status_code == 500
    html_path, csv_path = report_paths(pack_run.run_id)
    assert memory_blobs.exists(REPORTS_BUCKET, csv_path)
    assert not memory_blobs.exists(REPORTS_BUCKET, html_path)
    monkeypatch.setattr(memory_blobs, "put", put)
    later = frozen_clock + timedelta(minutes=5)
    move_clock(later)
    _ok(_finalise(client, pack_run))
    run = pack_run.repo.get_run(pack_run.run_id)
    assert run.finalised_at == later
    assert _printed_finalised_at(pack_run) == report.fmt_instant(later)
    assert len(fake_notifier.texts(CONTROLLER_CHAT)) == 1


def test_F10_R2_report_exists_before_freeze_is_conflict(
    pack_run: PackRun, api_client_as: Client, fake_notifier: FakeNotifier
) -> None:
    """Another finalise wrote report.html and has not yet set FINALISED: this caller must not
    freeze the run with an instant the report does not print."""
    html_path, _ = report_paths(pack_run.run_id)
    pack_run.blobs.put(REPORTS_BUCKET, html_path, b"<html>other</html>", "text/html")
    _error(_finalise(api_client_as("controller"), pack_run), 409, "RUN_STATE_CONFLICT")
    assert pack_run.repo.get_run(pack_run.run_id).status is RunStatus.AWAITING_REVIEW
    assert fake_notifier.sent == [] and _summaries(pack_run) == []


def test_F10_R7_overview_read_alerts_stale_summary_once(
    pack_run: PackRun, api_client_as: Client, fake_notifier: FakeNotifier, frozen_clock: datetime
) -> None:
    """C22: EP-05 of a finalised run runs the stale check too (no background jobs)."""
    _put_summary(pack_run, NotificationStatus.SENDING, frozen_clock - timedelta(minutes=5))
    client = api_client_as("reviewer")
    for _ in range(2):
        assert _ok(client.get(f"{API}/runs/{pack_run.run_id}"))["summary"]["status"] == "SENDING"
    assert len(fake_notifier.texts(OPS_CHAT)) == 1
    assert fake_notifier.texts(CONTROLLER_CHAT) == []


def test_F10_run_view_exceptions_skip_stale_open_items(pack_run: PackRun) -> None:
    """The one open-item rule (current_keys): an OPEN non-CELL item the newest reconcile no
    longer produces is not in `RunView.exceptions` (so not in the message or report)."""
    repo, run_id = pack_run.repo, pack_run.run_id
    stale = mapping_review(run_id, "MAP:ERP-0019:supplier").model_copy(
        update={"source_row_id": "REG-p2-r05", "field": "supplier"}
    )  # 4419's row; ranks before its CORRECTED item, so it would take over the line
    repo.upsert_review_items(run_id, [stale])
    every = report.exception_list(
        repo.get_run(run_id),
        repo.list_groups(run_id),
        repo.list_source_rows(run_id),
        repo.list_flags(run_id),
        repo.list_review_items(run_id),
        repo.get_proof(run_id),
    )
    view = load_run_view(repo, run_id)
    assert [e.text for e in every] != [e.text for e in view.exceptions]  # it would show
    assert "4419 qty corrected on register, 60 to 66" in [e.text for e in view.exceptions]
    assert _message(pack_run) == _golden(GOLDEN, pack_run)
