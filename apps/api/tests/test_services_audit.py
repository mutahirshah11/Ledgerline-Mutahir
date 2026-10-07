"""Audit helper (F-11.R6): one append-only entry at scenario-clock time."""

from datetime import datetime

from app.core.memory_repo import MemoryRepo
from app.domain.enums import AuditAction, AuditChannel
from app.services.audit import audit
from tests.factories import make_run


def test_audit_sets_run_id_actor_channel_and_clock_time(
    memory_repo: MemoryRepo, frozen_clock: datetime
) -> None:
    run = memory_repo.create_run(make_run())
    entry = audit(
        memory_repo,
        "controller@example.com",
        AuditAction.RUN_CREATED,
        "run",
        str(run.id),
        run_id=run.id,
        after={"status": "CREATED"},
        channel=AuditChannel.WEB,
    )
    assert entry.id > 0
    assert entry.at == frozen_clock
    assert (entry.actor, entry.action, entry.channel) == (
        "controller@example.com",
        AuditAction.RUN_CREATED,
        AuditChannel.WEB,
    )
    assert (entry.entity, entry.entity_id, entry.run_id) == ("run", str(run.id), run.id)
    assert (entry.before, entry.after) == (None, {"status": "CREATED"})
    assert memory_repo.list_audit(run.id) == [entry]

    system = audit(memory_repo, "system", AuditAction.ALERT_SENT, "notification", "n1")
    assert (system.channel, system.run_id) == (AuditChannel.SYSTEM, None)
