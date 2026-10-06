"""Run lifecycle matches data-model.md Section 6."""

from app.domain.enums import RunStatus as S
from app.domain.lifecycle import ALLOWED_TRANSITIONS, can_transition

NON_FINAL = [s for s in S if s not in (S.FINALISED, S.FAILED)]


def test_allowed_transitions_match_blueprint_section9() -> None:
    expected = {
        S.CREATED: {S.UPLOADED, S.FAILED},
        S.UPLOADED: {S.VALIDATED, S.FAILED},
        S.VALIDATED: {S.EXTRACTING, S.FAILED},
        S.EXTRACTING: {S.EXTRACTED, S.FAILED},
        S.EXTRACTED: {S.RECONCILED, S.FAILED},
        S.RECONCILED: {S.AWAITING_REVIEW, S.READY, S.FAILED},
        S.AWAITING_REVIEW: {S.AWAITING_REVIEW, S.READY, S.FINALISED, S.FAILED},
        S.READY: {S.AWAITING_REVIEW, S.READY, S.FINALISED, S.FAILED},
        S.FINALISED: set(),
        # retry returns to the status the failed stage started from (never the transient
        # RECONCILED, never FINALISED)
        S.FAILED: {
            S.CREATED, S.UPLOADED, S.VALIDATED, S.EXTRACTING, S.EXTRACTED,
            S.AWAITING_REVIEW, S.READY,
        },
    }
    assert set(ALLOWED_TRANSITIONS) == set(S)
    assert {k: set(v) for k, v in ALLOWED_TRANSITIONS.items()} == expected
    for src, targets in expected.items():
        for dst in S:
            assert can_transition(src, dst) is (dst in targets), (src, dst)


def test_finalised_has_no_outgoing() -> None:
    assert not ALLOWED_TRANSITIONS[S.FINALISED]
    assert not any(can_transition(S.FINALISED, dst) for dst in S)


def test_any_nonfinal_can_fail() -> None:
    for status in NON_FINAL:
        assert can_transition(status, S.FAILED), status
    assert not can_transition(S.FAILED, S.FAILED)


def test_finalise_only_from_review_states() -> None:
    sources = {s for s in S if can_transition(s, S.FINALISED)}
    assert sources == {S.AWAITING_REVIEW, S.READY}
