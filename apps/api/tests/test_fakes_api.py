"""Sanity checks for the Phase 3 test doubles (T009, contracts/auth.md, contracts/alerts.md)."""

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.errors import UpstreamFailed
from tests.fakes.api import (
    TEST_AUD,
    TEST_ISS,
    TEST_KID,
    FakeExtractor,
    FakeNotifier,
    FakeReconciler,
    fake_jwks_fetcher,
    make_token,
)


def _decode(token: str) -> dict[str, object]:
    jwks = fake_jwks_fetcher()
    header = jwt.get_unverified_header(token)
    (jwk,) = [k for k in jwks["keys"] if k["kid"] == header["kid"]]
    key = jwt.PyJWK(jwk).key
    return jwt.decode(token, key, algorithms=["ES256"], audience=TEST_AUD, issuer=TEST_ISS)


def test_token_verifies_with_fake_jwks() -> None:
    token = make_token("Controller@Kestrel.example")
    assert jwt.get_unverified_header(token) == {"alg": "ES256", "kid": TEST_KID, "typ": "JWT"}
    claims = _decode(token)
    assert claims["email"] == "Controller@Kestrel.example"
    assert claims["aud"] == "authenticated"


def test_token_options_change_claims() -> None:
    past = datetime(2020, 1, 1, tzinfo=UTC)
    token = make_token("a@b.example", exp=past, extra={"role": "authenticated"})
    with pytest.raises(jwt.ExpiredSignatureError):
        _decode(token)
    with pytest.raises(jwt.InvalidAudienceError):
        _decode(make_token("a@b.example", aud="anon"))
    with pytest.raises(jwt.InvalidIssuerError):
        _decode(make_token("a@b.example", iss="https://other.example/auth/v1"))
    other = make_token("a@b.example", key=_other_key(), exp=past + timedelta(days=10_000))
    with pytest.raises(jwt.InvalidSignatureError):
        _decode(other)


def _other_key() -> object:
    from cryptography.hazmat.primitives.asymmetric import ec

    return ec.generate_private_key(ec.SECP256R1())


def test_fake_notifier_records_and_fails() -> None:
    n = FakeNotifier()
    assert n.send_message("ops", "hello") == 1
    assert n.send_message("ops", "again") == 2
    assert n.sent == [("ops", "hello"), ("ops", "again")]
    n.fail = True
    with pytest.raises(UpstreamFailed) as info:
        n.send_message("ops", "x")
    assert info.value.service == "telegram"
    assert n.sent == [("ops", "hello"), ("ops", "again")]


def test_fake_stage_callables_record_calls() -> None:
    ext = FakeExtractor(result="page")
    assert ext(repo="r", page_no=2) == "page"
    assert ext.calls == [{"repo": "r", "page_no": 2}]
    rec = FakeReconciler(error=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        rec("repo", "inputs", faults=frozenset())
    assert rec.calls == [(("repo", "inputs"), {"faults": frozenset()})]
