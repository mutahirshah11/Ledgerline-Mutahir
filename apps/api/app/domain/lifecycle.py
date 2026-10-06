"""Run lifecycle (data-model.md Section 6). `Repo.transition_run` enforces this table."""

from types import MappingProxyType

from app.domain.enums import RunStatus as S

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
