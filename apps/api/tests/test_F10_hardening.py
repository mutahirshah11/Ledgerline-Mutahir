"""F-09/F-10 final review fixes, delivery side (005 review: I-1 to I-5, I-7, M-1 to M-8;
F-10.R5 to R7, F-09.R5, FM-10 to FM-12, research R9, R12, contracts/alerts.md).

Pack tests start from `pack_run` (reconciled to `AWAITING_REVIEW`, proof PASS, clock frozen).
"""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import Settings, get_settings
from app.core.db import Repo
from app.core.errors import UpstreamFailed
from app.core.storage import REPORTS_BUCKET
from app.domain.enums import (
    AuditAction,
    AuditChannel,
    NotificationKind,
    NotificationStatus,
    Outcome,
    ReviewKind,
    ReviewReason,
    ReviewStatus,
    RunStatus,
    Source,
    Stage,
)
from app.domain.ids import summary_key
from app.domain.models import (
    FinaliseRequest,
    Notification,
    ReviewItem,
    Run,
    UnknownMappingContext,
)
from app.pipeline import report
from app.services import alerts, telegram
from app.services import finalise as finalise_service
from app.services.finalise import load_run_view, report_paths
from tests.factories import make_group, make_review, make_row, make_run
from tests.fakes.api import FakeNotifier
from tests.support.pack_run import CONTROLLER, PackRun

API = "/api/v1"
Client = Callable[[str], TestClient]
CONTROLLER_CHAT = "controller-chat"
OPS_CHAT = "ops-chat"
CONFIRM = {"confirm": True}
MAY_HAVE = "Controller MAY have been messaged. Check the controller chat before any resend: "
GOT_MESSAGE = "Controller got the message but not report.html. Resend from "
NOT_MESSAGED = "Controller was NOT messaged. Retry from "


@pytest.fixture(autouse=True)
def chats(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CONTROLLER_CHAT_ID", CONTROLLER_CHAT)
    monkeypatch.setenv("TELEGRAM_OPS_CHAT_ID", OPS_CHAT)
    get_settings.cache_clear()


def _finalise(client: TestClient, pack: PackRun) -> Any:
    return client.post(f"{API}/runs/{pack.run_id}/finalise", json=CONFIRM)


def _alerts(pack: PackRun) -> list[Notification]:
    return [
        n for n in pack.repo.list_notifications(pack.run_id) if n.kind is NotificationKind.ALERT
    ]


def _finalised(pack: PackRun) -> Run:
    pack.repo.transition_run(
        pack.run_id,
        RunStatus.FINALISED,
        expected={RunStatus.AWAITING_REVIEW},
        finalised_by=CONTROLLER.email,
        finalised_at=pack.repo.get_run(pack.run_id).scenario_now,
    )
    return pack.repo.get_run(pack.run_id)


def _url(pack: PackRun) -> str:
    return f"{get_settings().APP_BASE_URL.rstrip('/')}/runs/{pack.run_id}"


# --- I-1: a stale outcomes.csv is never frozen with a fresh report.html -------------------


def test_F09_R5_stale_csv_without_html_is_409_and_alerts_once(
    pack_run: PackRun,
    api_client_as: Client,
    fake_notifier: FakeNotifier,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blobs = pack_run.blobs
    html_path, csv_path = report_paths(pack_run.run_id)
    put = blobs.put

    def no_html(bucket: str, path: str, data: bytes, content_type: str) -> None:
        if path == html_path:
            raise OSError("disk full")
        put(bucket, path, data, content_type)

    monkeypatch.setattr(blobs, "put", no_html)
    client = api_client_as("controller")
    assert _finalise(client, pack_run).status_code == 500
    assert blobs.exists(REPORTS_BUCKET, csv_path) and not blobs.exists(REPORTS_BUCKET, html_path)
    monkeypatch.setattr(blobs, "put", put)
    items = {i.item_key: i.id for i in pack_run.repo.list_review_items(pack_run.run_id)}
    body = {"action": "ENTER_VALUE", "value": "250"}
    resolved = client.post(f"{API}/review-items/{items['CELL:REG-p2-r14:qty']}/resolve", json=body)
    assert resolved.status_code == 200, resolved.text
    for _ in range(2):
        resp = _finalise(client, pack_run)
        assert resp.status_code == 409, resp.text
        assert "remove report.html and outcomes.csv" in resp.json()["error"]["message"]
    assert not blobs.exists(REPORTS_BUCKET, html_path)
    assert pack_run.repo.get_run(pack_run.run_id).status is RunStatus.AWAITING_REVIEW
    keys = [a.idempotency_key for a in _alerts(pack_run)]
    assert keys.count(f"ALERT:{pack_run.run_id}:finalise:orphan-report") == 1
    assert "remove report.html and outcomes.csv" in fake_notifier.texts(OPS_CHAT)[-1]
    assert fake_notifier.texts(CONTROLLER_CHAT) == []


# --- I-2, M-2: the report's "still open" table follows the one open-item rule ------------

RID = uuid4()


def _stale_mapping() -> ReviewItem:
    return make_review(
        RID,
        "MAP:ERP-0050:supplier_code",
        kind=ReviewKind.UNKNOWN_MAPPING,
        reason=ReviewReason.UNKNOWN_SUPPLIER,
        grn_no=4450,
        source_row_id="ERP-0050",
        field="supplier_code",
        read_a=None,
        read_b=None,
        context=UnknownMappingContext(source=Source.ERP, raw="ACME"),
    )


def test_F09_R3_still_open_table_follows_open_item_rule() -> None:
    run = make_run(id=RID)
    group = make_group(
        RID,
        "GRN:4450",
        grn_no=4450,
        outcome=Outcome.NEEDS_REVIEW,
        row_ids={Source.ERP: ["ERP-0050"]},
    )
    rows = [make_row(RID, "ERP-0050", grn_no=4450, source=Source.ERP)]
    stale, cell_item = _stale_mapping(), make_review(RID, "CELL:ERP-0050:qty", grn_no=4450)
    live = make_review(RID, "CELL:REG-p1-r01:qty")
    items = [stale, cell_item, live]
    keys = frozenset({live.item_key})
    ctx = report.build_report_context(
        run, [group], rows, [], items, None, [], [], [], current_keys=keys
    )
    assert [d.item.item_key for d in ctx.decisions] == [live.item_key]
    # M-2: CELL items too follow `current_keys` when it is given
    texts = [e.text for e in ctx.exceptions]
    assert texts == ["4450 needs review"]
    every = report.build_report_context(run, [group], rows, [], items, None, [], [], [])
    assert len(every.decisions) == 3


# --- M-8: EP-11 rows by Source order, not jsonb key order ---------------------------------


def test_F09_R3_group_views_rows_in_source_order() -> None:
    group = make_group(
        RID,
        "GRN:4450",
        grn_no=4450,
        row_ids={Source.SHEET: ["XLS-r50"], Source.REGISTER: ["REG-p1-r01"], Source.ERP: []},
    )
    rows = [
        make_row(RID, "XLS-r50", grn_no=4450, source=Source.SHEET),
        make_row(RID, "REG-p1-r01", grn_no=4450),
    ]
    (view,) = report.group_views([group], rows, [])
    assert list(view.rows) == [Source.REGISTER, Source.ERP, Source.SHEET]


# --- I-3, M-4: once-mode only for the summary ---------------------------------------------

TOKEN = "".join(["987654", ":", "TEST", "x" * 30])


def _real(*answers: httpx.Response | Exception) -> tuple[telegram.TelegramNotifier, list[str]]:
    calls: list[str] = []
    queue = list(answers)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path.rsplit("/", 1)[-1])
        answer = (
            queue.pop(0)
            if queue
            else httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})
        )
        if isinstance(answer, Exception):
            raise answer
        return answer

    settings = Settings(_env_file=None, TELEGRAM_BOT_TOKEN=SecretStr(TOKEN))
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return telegram.TelegramNotifier(settings, client=client, sleep=lambda _: None), calls


def test_F11_R4_alert_retries_5xx_summary_does_not() -> None:
    alert, calls = _real(httpx.Response(502))
    assert alert.send_message("ops", "alert") == 7
    assert calls == ["sendMessage", "sendMessage"]
    summary, calls = _real(httpx.Response(502))
    with pytest.raises(telegram.MaybeDelivered):
        summary.send_message("ctl", "summary", at_most_once=True)
    assert calls == ["sendMessage"]


def test_F10_R7_unexpected_httpx_error_once_is_maybe_delivered() -> None:
    summary, _ = _real(httpx.DecodingError("bad gzip"))
    with pytest.raises(telegram.MaybeDelivered) as info:
        summary.send_message("ctl", "summary", at_most_once=True)
    assert TOKEN not in str(info.value)


def test_F10_R5_finalise_sends_summary_once_mode_alerts_not(
    pack_run: PackRun, api_client_as: Client, fake_notifier: FakeNotifier
) -> None:
    fake_notifier.fail_document = True
    assert _finalise(api_client_as("controller"), pack_run).status_code == 200
    assert fake_notifier.once == [(CONTROLLER_CHAT, True), (OPS_CHAT, False)]


# --- I-4: the alert's last line says what the controller got -------------------------------


def _summary(pack: PackRun, run: Run, **over: Any) -> Notification:
    assert run.run_key is not None
    base: dict[str, Any] = dict(
        id=uuid4(),
        run_id=run.id,
        kind=NotificationKind.SUMMARY,
        idempotency_key=summary_key(run.run_key),
        status=NotificationStatus.PENDING,
        attempts=0,
        last_error=None,
        telegram_message_id=None,
        sent_at=None,
        updated_at=run.scenario_now,
    )
    n, created = pack.repo.insert_notification(Notification(**(base | over)))
    assert created
    return n


class _Unsure(FakeNotifier):
    def send_message(self, chat_id: str, text: str, **kwargs: Any) -> int:
        if chat_id == CONTROLLER_CHAT:
            raise telegram.MaybeDelivered("telegram", "timeout, maybe delivered")
        return super().send_message(chat_id, text, **kwargs)


def test_F11_R4_maybe_delivered_alert_tail(pack_run: PackRun) -> None:
    run = _finalised(pack_run)
    notifier = _Unsure()
    n = telegram.send_summary(pack_run.repo, notifier, run, _summary(pack_run, run), "t", b"x")
    assert n.status is NotificationStatus.SENDING
    (alert,) = notifier.texts(OPS_CHAT)
    assert alert.splitlines()[-1] == MAY_HAVE + _url(pack_run)


def test_F11_R4_stale_alert_tail(pack_run: PackRun, frozen_clock: datetime) -> None:
    run = _finalised(pack_run)
    old = frozen_clock - timedelta(minutes=5)
    _summary(pack_run, run, status=NotificationStatus.SENDING, updated_at=old)
    notifier = FakeNotifier()
    assert telegram.check_stale_summary(pack_run.repo, notifier, run.id) is not None
    (alert,) = notifier.texts(OPS_CHAT)
    assert alert.splitlines()[-1] == MAY_HAVE + _url(pack_run)


def test_F11_R4_document_only_failure_alert_tail(pack_run: PackRun) -> None:
    run = _finalised(pack_run)
    notifier = FakeNotifier(fail_document=True)
    n = telegram.send_summary(pack_run.repo, notifier, run, _summary(pack_run, run), "t", b"x")
    assert n.status is NotificationStatus.FAILED and n.telegram_message_id is not None
    (alert,) = notifier.texts(OPS_CHAT)
    assert alert.splitlines()[-1] == GOT_MESSAGE + _url(pack_run)


def test_F11_R4_message_failure_alert_tail_unchanged(pack_run: PackRun) -> None:
    run = _finalised(pack_run)

    class _NoController(FakeNotifier):
        def send_message(self, chat_id: str, text: str, **kwargs: Any) -> int:
            if chat_id == CONTROLLER_CHAT:
                raise UpstreamFailed("telegram", "rejected: HTTP 400")
            return super().send_message(chat_id, text, **kwargs)

    notifier = _NoController()
    telegram.send_summary(pack_run.repo, notifier, run, _summary(pack_run, run), "t", b"x")
    (alert,) = notifier.texts(OPS_CHAT)
    assert alert.splitlines()[-1] == NOT_MESSAGED + _url(pack_run)
    custom = alerts.alert_text(run, Stage.NOTIFY, ValueError(), None, "https://x", tail="T {url}")
    assert custom.splitlines()[-1] == f"T https://x/runs/{run.id}"


# --- I-5: nothing sent, so never stranded in SENDING --------------------------------------


def test_F10_R7_failure_before_any_send_is_failed_not_sending(
    pack_run: PackRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _finalised(pack_run)
    real = pack_run.repo.add_run_event
    failed: list[bool] = []

    def flaky(event: Any) -> Any:
        if event.stage is Stage.NOTIFY and not failed:
            failed.append(True)
            raise OSError("connection reset")
        return real(event)

    monkeypatch.setattr(pack_run.repo, "add_run_event", flaky)
    notifier = FakeNotifier()
    n = telegram.send_summary(pack_run.repo, notifier, run, _summary(pack_run, run), "t", b"x")
    assert n.status is NotificationStatus.FAILED
    assert notifier.texts(CONTROLLER_CHAT) == []
    assert len(notifier.texts(OPS_CHAT)) == 1
    resent = telegram.send_summary(pack_run.repo, notifier, run, n, "t", b"x")
    assert resent.status is NotificationStatus.SENT
    assert notifier.texts(CONTROLLER_CHAT) == ["t"]


# --- M-5: a document-only resend does not rebuild the text --------------------------------


def test_F10_R6_document_only_resend_skips_text(
    pack_run: PackRun,
    api_client_as: Client,
    fake_notifier: FakeNotifier,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = api_client_as("controller")
    fake_notifier.fail_document = True
    assert _finalise(client, pack_run).json()["notification"]["status"] == "FAILED"
    fake_notifier.fail_document = False

    def broken(*args: Any, **kwargs: Any) -> telegram.SummaryMessage:
        raise telegram.MessageTooLong("21 lines")

    monkeypatch.setattr(telegram, "build_summary_message", broken)
    body = _finalise(client, pack_run).json()
    assert body["notification"]["status"] == "SENT"
    assert len(fake_notifier.texts(CONTROLLER_CHAT)) == 1
    assert len(fake_notifier.documents_to(CONTROLLER_CHAT)) == 1


# --- M-6: adoption is audited ----------------------------------------------------------------


def test_F10_R9_adopted_report_audit_says_adopted(
    pack_run: PackRun, api_client_as: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    transition = pack_run.repo.transition_run
    failed: list[bool] = []

    def flaky(run_id: Any, to: RunStatus, **kwargs: Any) -> Run:
        if to is RunStatus.FINALISED and not failed:
            failed.append(True)
            raise OSError("connection reset")
        return transition(run_id, to, **kwargs)

    monkeypatch.setattr(pack_run.repo, "transition_run", flaky)
    client = api_client_as("controller")
    assert _finalise(client, pack_run).status_code == 500
    assert _finalise(client, pack_run).status_code == 200
    (entry,) = [
        a
        for a in pack_run.repo.list_audit(run_id=pack_run.run_id)
        if a.action is AuditAction.RUN_FINALISED
    ]
    assert entry.after is not None and entry.after["adopted"] is True


def test_F10_R2_fresh_finalise_audit_has_no_adopted(
    pack_run: PackRun, api_client_as: Client
) -> None:
    assert _finalise(api_client_as("controller"), pack_run).status_code == 200
    (entry,) = [
        a
        for a in pack_run.repo.list_audit(run_id=pack_run.run_id)
        if a.action is AuditAction.RUN_FINALISED
    ]
    assert entry.after is not None and "adopted" not in entry.after


# --- I-7: repo round trips of EP-17 and EP-05 ---------------------------------------------


class _Counting:
    """Counts every repo method call."""

    def __init__(self, inner: Repo) -> None:
        self._inner = inner
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def counted(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            return attr(*args, **kwargs)

        return counted


EP17_MAX = 35
EP05_MAX = 14


def test_F10_NFR_finalise_repo_round_trips(pack_run: PackRun) -> None:
    counting = _Counting(pack_run.repo)
    repo = cast(Repo, counting)
    finalise_service.finalise(
        repo,
        pack_run.blobs,
        FakeNotifier(),
        pack_run.run_id,
        FinaliseRequest(confirm=True),
        CONTROLLER,
        AuditChannel.WEB,
    )
    assert pack_run.repo.get_run(pack_run.run_id).status is RunStatus.FINALISED
    assert len(counting.calls) <= EP17_MAX, (len(counting.calls), counting.calls)


def test_F10_NFR_run_view_repo_round_trips(pack_run: PackRun) -> None:
    counting = _Counting(pack_run.repo)
    view = load_run_view(cast(Repo, counting), pack_run.run_id)
    assert view.open_review_items == 4
    assert len(counting.calls) <= EP05_MAX, (len(counting.calls), counting.calls)
    assert counting.calls.count("list_audit") == 1


# --- IMP-1, IMP-2: a decision not yet reconciled is never frozen ---------------------------

STILL_APPLYING = "a review decision is still being applied; try again"
CHANGED_MIDWAY = "a review decision landed while the report was being written"
REMOVE = "remove report.html and outcomes.csv"
QTY_4419 = "CELL:REG-p2-r05:qty"


def _decide_unreconciled(pack: PackRun) -> None:
    """EP-15's decision write without its re-reconcile (the gap IMP-1 is about)."""
    from app.services.review import accept_read_value

    repo = pack.repo
    (item,) = [i for i in repo.list_review_items(pack.run_id) if i.item_key == QTY_4419]
    repo.resolve_review_item(
        item.id,
        status=ReviewStatus.RESOLVED,
        resolved_value=accept_read_value(item),
        resolved_by="reviewer@example.com",
        resolved_at=pack.run.scenario_now,
        note=None,
    )


def _reconcile(pack: PackRun) -> None:
    from app.services.stages import reconcile_stage

    run = pack.repo.get_run(pack.run_id)
    reconcile_stage(pack.repo, pack.blobs, FakeNotifier(), run, CONTROLLER, None)


def _error(resp: Any) -> str:
    assert resp.status_code == 409, resp.text
    error = resp.json()["error"]
    assert error["code"] == "RUN_STATE_CONFLICT"
    return str(error["message"]).lower()


def test_F10_R2_unreconciled_decision_is_409_until_reconciled(
    pack_run: PackRun, api_client_as: Client
) -> None:
    _decide_unreconciled(pack_run)
    client = api_client_as("controller")
    assert STILL_APPLYING in _error(_finalise(client, pack_run))
    html_path, csv_path = report_paths(pack_run.run_id)
    for path in (html_path, csv_path):
        assert not pack_run.blobs.exists(REPORTS_BUCKET, path)
    assert pack_run.repo.get_run(pack_run.run_id).status is RunStatus.AWAITING_REVIEW
    assert _alerts(pack_run) == []
    _reconcile(pack_run)
    assert _finalise(client, pack_run).status_code == 200


def test_F10_R2_decision_after_read_is_409_before_any_file(
    pack_run: PackRun, api_client_as: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last check runs right before the first file write: a decision landing after the
    read (here while the CSV is rendered) leaves no report file behind."""
    render = report.render_outcomes_csv
    landed: list[bool] = []

    def decide_then_render(*args: Any, **kwargs: Any) -> bytes:
        if not landed:
            landed.append(True)
            _decide_unreconciled(pack_run)
        return render(*args, **kwargs)

    monkeypatch.setattr(report, "render_outcomes_csv", decide_then_render)
    assert STILL_APPLYING in _error(_finalise(api_client_as("controller"), pack_run))
    for path in report_paths(pack_run.run_id):
        assert not pack_run.blobs.exists(REPORTS_BUCKET, path)
    assert _alerts(pack_run) == []


def test_F10_R2_finalise_again_after_the_mid_finalise_409(
    pack_run: PackRun,
    api_client_as: Client,
    fake_notifier: FakeNotifier,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """research R9: a decision committing between the last check and `FINALISED` (after the
    write-once report files) is refused (409, the run is not frozen); the files it left print
    the data before it, so once it is reconciled the next finalise is the orphan route: 409
    and one ops alert to remove both files (paths are fixed and write-once)."""
    blobs = pack_run.blobs
    html_path, _ = report_paths(pack_run.run_id)
    put = blobs.put

    def decide_after_html(bucket: str, path: str, data: bytes, content_type: str) -> None:
        put(bucket, path, data, content_type)
        if path == html_path:
            _decide_unreconciled(pack_run)

    monkeypatch.setattr(blobs, "put", decide_after_html)
    client = api_client_as("controller")
    assert CHANGED_MIDWAY in _error(_finalise(client, pack_run))
    monkeypatch.setattr(blobs, "put", put)
    assert blobs.exists(REPORTS_BUCKET, html_path)
    assert pack_run.repo.get_run(pack_run.run_id).status is RunStatus.AWAITING_REVIEW
    assert STILL_APPLYING in _error(_finalise(client, pack_run))  # not reconciled yet
    assert _alerts(pack_run) == []
    _reconcile(pack_run)
    for _ in range(2):
        message = _error(_finalise(client, pack_run))
        assert REMOVE in message and "changed since" in message
    assert pack_run.repo.get_run(pack_run.run_id).status is RunStatus.AWAITING_REVIEW
    keys = [a.idempotency_key for a in _alerts(pack_run)]
    assert keys == [f"ALERT:{pack_run.run_id}:finalise:orphan-report"]
    assert REMOVE in fake_notifier.texts(OPS_CHAT)[-1]
    assert fake_notifier.texts(CONTROLLER_CHAT) == []
