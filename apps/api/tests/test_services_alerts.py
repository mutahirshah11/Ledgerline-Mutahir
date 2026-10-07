"""Ops alerts on stage failure (T019; F-11.R4, F-11.R5, contracts/alerts.md, research R9)."""

from datetime import datetime, timedelta
from uuid import UUID

import pytest

from app.core.clock import KARACHI
from app.core.config import Settings
from app.core.errors import UpstreamFailed
from app.core.memory_repo import MemoryRepo
from app.domain.enums import (
    AuditAction,
    AuditChannel,
    ErrorCode,
    NotificationKind,
    NotificationStatus,
    RunStatus,
    Stage,
)
from app.domain.models import Run
from app.services.alerts import stage_failed_alert
from tests.factories import make_run
from tests.fakes.api import FakeNotifier

NOW = datetime(2026, 10, 15, 8, 30, 12, tzinfo=KARACHI)
RUN_ID = UUID("7f3c2b1e-0000-4000-8000-000000000001")
OPS, CONTROLLER = "-100111", "-100222"
TIMEOUT = UpstreamFailed("openai", "timeout after 3 attempts", stage="extract", page_no=2)


class _Coded(Exception):
    """Duck-typed coded error (code + message), like the API's ApiError."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        APP_BASE_URL="https://ledgerline.example",
        TELEGRAM_OPS_CHAT_ID=OPS,
        TELEGRAM_CONTROLLER_CHAT_ID=CONTROLLER,
    )


@pytest.fixture
def failed_run(memory_repo: MemoryRepo) -> Run:
    return memory_repo.create_run(
        make_run(
            id=RUN_ID,
            status=RunStatus.FAILED,
            failed_stage=Stage.EXTRACT,
            error_message="Page 2 could not be read",
        )
    )


def _alert(
    repo: MemoryRepo,
    notifier: FakeNotifier,
    run: Run,
    settings: Settings,
    *,
    stage: Stage = Stage.EXTRACT,
    error: BaseException = TIMEOUT,
    page_no: int | None = None,
    at: datetime = NOW,
) -> None:
    stage_failed_alert(
        repo, notifier, run, stage, error, page_no, settings=settings, now=lambda: at
    )


def test_F11_R4_alert_format(memory_repo: MemoryRepo, failed_run: Run, settings: Settings) -> None:
    notifier = FakeNotifier()
    _alert(memory_repo, notifier, failed_run, settings)
    problems = _Coded(ErrorCode.CONTRACT_VIOLATION, "2 problems (ERP: column lot_no is missing; …)")
    later = NOW + timedelta(minutes=1)
    _alert(
        memory_repo, notifier, failed_run, settings, stage=Stage.VALIDATE, error=problems, at=later
    )
    link = f"https://ledgerline.example/runs/{RUN_ID}"
    assert [text for _, text in notifier.sent] == [
        "LEDGERLINE FAILED · Sep 2026 · run 7f3c…\n"
        "Stage: extract (page 2)\n"
        "Error: UPSTREAM_FAILED: OpenAI timeout after 3 attempts\n"
        f"Controller was NOT messaged. Retry from {link}",
        "LEDGERLINE FAILED · Sep 2026 · run 7f3c…\n"
        "Stage: validate\n"
        "Error: CONTRACT_VIOLATION: 2 problems (ERP: column lot_no is missing; …)\n"
        f"Controller was NOT messaged. Retry from {link}",
    ]


def test_F11_R4_alert_page_argument_and_length_cap(
    memory_repo: MemoryRepo, failed_run: Run, settings: Settings
) -> None:
    notifier = FakeNotifier()
    long = _Coded(ErrorCode.CONTRACT_VIOLATION, "x" * 5000)
    _alert(memory_repo, notifier, failed_run, settings, error=long, page_no=1)
    ((_, text),) = notifier.sent
    assert len(text) <= 4096
    assert "Stage: extract (page 1)\n" in text
    assert text.endswith(f"Retry from https://ledgerline.example/runs/{RUN_ID}")


def test_F11_R4_same_minute_alerts_once(
    memory_repo: MemoryRepo, failed_run: Run, settings: Settings
) -> None:
    notifier = FakeNotifier()
    _alert(memory_repo, notifier, failed_run, settings)
    _alert(memory_repo, notifier, failed_run, settings, at=NOW + timedelta(seconds=40))
    assert len(notifier.sent) == 1
    (n,) = memory_repo.list_notifications(RUN_ID)
    assert n.kind is NotificationKind.ALERT
    assert n.idempotency_key == f"ALERT:{RUN_ID}:extract:202610150830"


def test_F11_R4_new_minute_alerts_again(
    memory_repo: MemoryRepo, failed_run: Run, settings: Settings
) -> None:
    notifier = FakeNotifier()
    _alert(memory_repo, notifier, failed_run, settings)
    _alert(memory_repo, notifier, failed_run, settings, at=NOW + timedelta(minutes=1))
    assert len(notifier.sent) == 2
    keys = sorted(n.idempotency_key for n in memory_repo.list_notifications(RUN_ID))
    assert keys == [f"ALERT:{RUN_ID}:extract:202610150830", f"ALERT:{RUN_ID}:extract:202610150831"]


def test_F11_R4_alert_never_uses_controller_chat(
    memory_repo: MemoryRepo, failed_run: Run, settings: Settings
) -> None:
    notifier = FakeNotifier()
    for i, stage in enumerate([Stage.VALIDATE, Stage.EXTRACT, Stage.RECONCILE, Stage.PROOF]):
        _alert(
            memory_repo, notifier, failed_run, settings, stage=stage, at=NOW + timedelta(minutes=i)
        )
    assert [chat for chat, _ in notifier.attempted] == [OPS] * 4


def test_F11_R5_alert_failure_recorded_on_run(
    memory_repo: MemoryRepo, failed_run: Run, settings: Settings
) -> None:
    _alert(memory_repo, FakeNotifier(fail=True), failed_run, settings)  # never raises
    (n,) = memory_repo.list_notifications(RUN_ID)
    assert n.status is NotificationStatus.FAILED
    assert n.attempts == 1
    assert n.last_error is not None and n.last_error.startswith("UPSTREAM_FAILED")
    assert n.updated_at == NOW
    run = memory_repo.get_run(RUN_ID)
    assert (
        run.error_message == "Page 2 could not be read; ops alert not delivered (UPSTREAM_FAILED)"
    )
    assert run.status is RunStatus.FAILED and run.failed_stage is Stage.EXTRACT
    assert memory_repo.list_audit(run_id=RUN_ID) == []


def test_F11_R5_alert_unset_ops_chat_recorded_not_sent(
    memory_repo: MemoryRepo, failed_run: Run
) -> None:
    notifier = FakeNotifier()
    unset = Settings(_env_file=None, TELEGRAM_CONTROLLER_CHAT_ID=CONTROLLER)
    _alert(memory_repo, notifier, failed_run, unset)
    assert notifier.attempted == []
    (n,) = memory_repo.list_notifications(RUN_ID)
    assert n.status is NotificationStatus.FAILED
    assert memory_repo.get_run(RUN_ID).error_message is not None
    assert "ops alert not delivered" in (memory_repo.get_run(RUN_ID).error_message or "")


def test_alert_sent_audited_system_telegram(
    memory_repo: MemoryRepo, failed_run: Run, settings: Settings
) -> None:
    _alert(memory_repo, FakeNotifier(), failed_run, settings)
    (n,) = memory_repo.list_notifications(RUN_ID)
    assert (n.status, n.telegram_message_id, n.sent_at, n.attempts) == (
        NotificationStatus.SENT,
        1,
        NOW,
        1,
    )
    (entry,) = memory_repo.list_audit(run_id=RUN_ID)
    assert entry.action is AuditAction.ALERT_SENT
    assert (entry.actor, entry.channel) == ("system", AuditChannel.TELEGRAM)
    assert (entry.entity, entry.entity_id, entry.at) == ("notification", str(n.id), NOW)
    assert memory_repo.get_run(RUN_ID).error_message == "Page 2 could not be read"
