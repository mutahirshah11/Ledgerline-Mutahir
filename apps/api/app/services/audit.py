"""Audit helper (F-11.R6): one append-only `AuditEntry` at scenario-clock time."""

from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID

from app.core import clock
from app.core.db import Repo
from app.domain.enums import AuditAction, AuditChannel
from app.domain.models import AuditEntry


def audit(
    repo: Repo,
    actor: str,
    action: AuditAction,
    entity: str,
    entity_id: str,
    run_id: UUID | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    channel: AuditChannel = AuditChannel.SYSTEM,
    *,
    now: Callable[[], datetime] = clock.now,
) -> AuditEntry:
    """Append and return the stored entry; `actor` is an email or "system"."""
    return repo.append_audit(
        AuditEntry(
            id=0,  # assigned by the repo
            at=now(),
            actor=actor,
            action=action,
            entity=entity,
            entity_id=entity_id,
            run_id=run_id,
            before=before,
            after=after,
            channel=channel,
        )
    )
