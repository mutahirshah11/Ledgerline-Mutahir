"""Phase 4 consumes these Phase 1 to 3 interfaces (005 T002, plan.md "Consumed interfaces").

Real names differ slightly from the 005 docs: `require_role` (not `require_roles`),
`alerts.stage_failed_alert` (not `send_ops_alert`), `Notifier.send_message` (not `send_text`).
"""

import inspect

from app.core import auth
from app.core.db import Repo
from app.domain.enums import Fault, RunStatus
from app.pipeline import engine
from app.services import alerts, stages
from app.services.telegram import Notifier, TelegramNotifier


def _params(fn: object) -> list[str]:
    return list(inspect.signature(fn).parameters)  # type: ignore[arg-type]


def test_reconcile_stage_accepts_review_and_ready_runs() -> None:
    assert _params(stages.reconcile_stage)[:6] == [
        "repo", "blobs", "notifier", "run", "user", "fault"
    ]  # fmt: skip
    assert {RunStatus.AWAITING_REVIEW, RunStatus.READY} <= set(engine.RECONCILABLE)


def test_auth_alerts_engine_helpers() -> None:
    assert callable(auth.require_role)
    assert _params(alerts.stage_failed_alert)[:5] == ["repo", "notifier", "run", "stage", "error"]
    assert _params(engine.count_open_items) == ["open_items", "current_keys"]
    assert _params(engine.open_review_count) == ["repo", "run_id"]
    assert _params(stages.run_outcome_counts) == ["repo", "run_id"]


def test_notifier_sends_text_and_document() -> None:
    for cls in (Notifier, TelegramNotifier):
        assert _params(cls.send_message) == ["self", "chat_id", "text", "on_retry"]
        assert _params(cls.send_document)[:5] == [
            "self",
            "chat_id",
            "filename",
            "content",
            "caption",
        ]


def test_faults_and_repo_methods() -> None:
    assert {Fault.TELEGRAM_DOWN, Fault.PROOF_DROP_ROW} <= set(Fault)
    for name in (
        "resolve_review_item",
        "insert_notification",
        "update_notification",
        "list_stale_sending",
        "get_notification_by_key",
    ):
        assert callable(getattr(Repo, name)), name
