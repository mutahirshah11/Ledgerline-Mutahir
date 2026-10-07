"""Phase 3 test doubles (T009): notifier, signed test tokens with a fake JWKS, stage fakes.

contracts/alerts.md (FakeNotifier), contracts/auth.md and research R1 (EC P-256 key generated at
import, so no key material is ever committed), plan.md "Consumed interfaces" (FakeExtractor,
FakeReconciler stand in for `extract_page` and `reconcile_run`).
"""

from datetime import UTC, datetime
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

from app.core.errors import UpstreamFailed

# --- Notifier ------------------------------------------------------------------------------


class FakeNotifier:
    """Records `(chat_id, text)` per sent message; with `fail` set, raises UpstreamFailed."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple[str, str]] = []
        self.attempted: list[tuple[str, str]] = []

    def send_message(self, chat_id: str, text: str) -> int:
        """Returns the next message id (1, 2, ...), like Telegram's `message_id`."""
        self.attempted.append((chat_id, text))
        if self.fail:
            raise UpstreamFailed("telegram", "down after 3 attempts")
        self.sent.append((chat_id, text))
        return len(self.sent)


# --- Tokens and JWKS (contracts/auth.md) ---------------------------------------------------

TEST_SUPABASE_URL = "https://ledgerline-test.supabase.co"
"""Set `SUPABASE_URL` to this in auth tests so `TEST_ISS` is the expected issuer."""
TEST_ISS = f"{TEST_SUPABASE_URL}/auth/v1"
TEST_AUD = "authenticated"
TEST_KID = "ledgerline-test-key"
TEST_EXP = datetime(2099, 1, 1, tzinfo=UTC)
"""Default expiry: fixed and far in the future (tests never read the real clock)."""

TEST_PRIVATE_KEY = ec.generate_private_key(ec.SECP256R1())


def public_jwk(
    key: ec.EllipticCurvePrivateKey = TEST_PRIVATE_KEY, kid: str = TEST_KID
) -> dict[str, Any]:
    """The public half of `key` as a JWK dict with `kid`, `alg` and `use`."""
    jwk: dict[str, Any] = ECAlgorithm.to_jwk(key.public_key(), as_dict=True)
    return jwk | {"kid": kid, "alg": "ES256", "use": "sig"}


TEST_JWKS: dict[str, Any] = {"keys": [public_jwk()]}


def make_token(
    email: str | None,
    *,
    exp: datetime | None = None,
    iss: str = TEST_ISS,
    aud: str = TEST_AUD,
    alg: str = "ES256",
    kid: str = TEST_KID,
    key: Any = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """A signed access token like Supabase's. `email=None` omits the claim; `key` defaults to
    the test key (pass another key, or an HS256 secret with `alg="HS256"`, for bad tokens)."""
    claims: dict[str, Any] = {"iss": iss, "aud": aud, "exp": exp or TEST_EXP, "sub": "test-user"}
    if email is not None:
        claims["email"] = email
    claims |= extra or {}
    return jwt.encode(
        claims, key if key is not None else TEST_PRIVATE_KEY, algorithm=alg, headers={"kid": kid}
    )


def fake_jwks_fetcher(url: str = "") -> dict[str, Any]:
    """Returns the test JWKS for any URL."""
    return {"keys": [dict(k) for k in TEST_JWKS["keys"]]}


class FakeJwksFetcher:
    """Callable JWKS fetcher that counts calls; `keys` may be swapped and `fail` set to an
    exception to raise (e.g. UpstreamFailed("supabase", ...) for FM-14)."""

    def __init__(
        self, keys: list[dict[str, Any]] | None = None, *, fail: BaseException | None = None
    ) -> None:
        self.keys = keys if keys is not None else [public_jwk()]
        self.fail = fail
        self.calls: list[str] = []

    def __call__(self, url: str = "") -> dict[str, Any]:
        self.calls.append(url)
        if self.fail is not None:
            raise self.fail
        return {"keys": [dict(k) for k in self.keys]}


# --- Stage fakes (plan.md "Consumed interfaces") -------------------------------------------


class FakeExtractor:
    """Stands in for `app.pipeline.extract.extract_page` (keyword-only); records each call's
    keyword arguments; returns `result` or raises `error`."""

    def __init__(self, result: Any = None, *, error: BaseException | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


class FakeReconciler:
    """Stands in for `app.pipeline.engine.reconcile_run(repo, inputs, *, now, faults)`; records
    `(args, kwargs)`; returns `result` or raises `error`."""

    def __init__(self, result: Any = None, *, error: BaseException | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error
        return self.result
