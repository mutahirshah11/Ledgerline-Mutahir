"""Run lifecycle (data-model.md Section 6). `Repo.transition_run` enforces this table."""

from collections.abc import Sequence
from types import MappingProxyType

from app.domain.enums import AuditAction, ReviewStatus
from app.domain.enums import RunStatus as S
from app.domain.models import AuditEntry, ReviewItem

_REVIEW_STATES = frozenset({S.AWAITING_REVIEW, S.READY})

ALLOWED_TRANSITIONS: MappingProxyType[S, frozenset[S]] = MappingProxyType(
    {
        S.CREATED: frozenset({S.UPLOADED, S.FAILED}),
        S.UPLOADED: frozenset({S.VALIDATED, S.FAILED}),
        S.VALIDATED: frozenset({S.EXTRACTING, S.FAILED}),
        S.EXTRACTING: frozenset({S.EXTRACTED, S.FAILED}),
        S.EXTRACTED: frozenset({S.RECONCILED, S.FAILED}),
        S.RECONCILED: _REVIEW_STATES | {S.FAILED},
        # re-reconcile keeps or swaps the review state; Finalise needs proof PASS (caller checks)
        S.AWAITING_REVIEW: _REVIEW_STATES | {S.FINALISED, S.FAILED},
        S.READY: _REVIEW_STATES | {S.FINALISED, S.FAILED},
        S.FINALISED: frozenset(),  # read-only
        # retry returns to the status the failed stage started from; RECONCILED exists only
        # inside one engine call, so it is never a retry target
        S.FAILED: frozenset(
            {S.CREATED, S.UPLOADED, S.VALIDATED, S.EXTRACTING, S.EXTRACTED} | _REVIEW_STATES
        ),
    }
)


def can_transition(current: S, to: S) -> bool:
    """True when the lifecycle allows moving a run from `current` to `to`."""
    return to in ALLOWED_TRANSITIONS[current]


DECIDED_KEYS = "decided_item_keys"
"""`RECONCILED` audit `after` key: the item keys of the decided (not OPEN) review items that
reconcile read as its inputs, i.e. the decisions its rows, groups and proof apply (IMP-1)."""


def unreconciled_decisions(
    items: Sequence[ReviewItem], audit: Sequence[AuditEntry]
) -> list[ReviewItem]:
    """The decided items no reconcile has applied yet (F-10.R2, research R9): those whose key
    is missing from the newest `RECONCILED` audit's `decided_item_keys`. Newest is the highest
    id (append order; `at` can tie under a frozen clock); an entry without the list, or no
    entry, applied none. A run with any is not finalised (`Repo.transition_run`, migration
    0003 `runs_refuse_unreconciled_finalise`). A decision is never undone, so the rule only
    needs the keys."""
    newest = max(
        (a for a in audit if a.action is AuditAction.RECONCILED), key=lambda a: a.id, default=None
    )
    keys = None if newest is None or newest.after is None else newest.after.get(DECIDED_KEYS)
    applied = set(keys) if isinstance(keys, list) else set()
    return [i for i in items if i.status is not ReviewStatus.OPEN and i.item_key not in applied]
