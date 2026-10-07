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
from app.services.runlog import OnRetry

# --- Notifier ------------------------------------------------------------------------------


class FakeNotifier:
    """Records what it delivers, per chat id: `sent` `(chat_id, text)` and `documents`
    `(chat_id, filename, content, caption)`; `attempted` / `attempted_documents` also hold the
    failed calls. Delivered ids increase across texts and documents (1, 2, ...), like
    Telegram's `message_id`. `down` (alias `fail`, the Phase 3 name) makes every call raise
    UpstreamFailed("telegram", ...) as the real notifier does after its retries;
    `fail_document` fails documents only (005 T009, research R17); `once` records each
    `send_message`'s `(chat_id, at_most_once)`. It has no retries of its own, so it never
    calls `on_retry`."""

    def __init__(
        self, *, fail: bool = False, down: bool = False, fail_document: bool = False
    ) -> None:
        self.down = down or fail
        self.fail_document = fail_document
        self.sent: list[tuple[str, str]] = []
        self.attempted: list[tuple[str, str]] = []
        self.documents: list[tuple[str, str, bytes, str | None]] = []
        self.attempted_documents: list[tuple[str, str, bytes, str | None]] = []
        self.once: list[tuple[str, bool]] = []
        self._last_id = 0

    @property
    def fail(self) -> bool:
        return self.down

    @fail.setter
    def fail(self, value: bool) -> None:
        self.down = value

    def _next_id(self) -> int:
        self._last_id += 1
        return self._last_id

    def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        on_retry: OnRetry | None = None,
        at_most_once: bool = False,
    ) -> int:
        self.attempted.append((chat_id, text))
        self.once.append((chat_id, at_most_once))
        if self.down:
            raise UpstreamFailed("telegram", "down after 3 attempts")
        self.sent.append((chat_id, text))
        return self._next_id()

    def send_document(
        self,
        chat_id: str,
        filename: str,
        content: bytes,
        caption: str | None = None,
        *,
        on_retry: OnRetry | None = None,
    ) -> int:
        self.attempted_documents.append((chat_id, filename, content, caption))
        if self.down or self.fail_document:
            raise UpstreamFailed("telegram", "down after 3 attempts")
        self.documents.append((chat_id, filename, content, caption))
        return self._next_id()

    def texts(self, chat_id: str) -> list[str]:
        """Delivered texts to `chat_id`, in order."""
        return [text for chat, text in self.sent if chat == chat_id]

    def documents_to(self, chat_id: str) -> list[tuple[str, bytes]]:
        """Delivered `(filename, content)` to `chat_id`, in order."""
        return [(name, data) for chat, name, data, _ in self.documents if chat == chat_id]


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
    omit: tuple[str, ...] = (),
) -> str:
    """A signed access token like Supabase's. `email=None` omits the claim, as does naming any
    claim in `omit`; `key` defaults to the test key (pass another key, or an HS256 secret with
    `alg="HS256"`, for bad tokens); `alg="none"` gives an unsigned token."""
    claims: dict[str, Any] = {"iss": iss, "aud": aud, "exp": exp or TEST_EXP, "sub": "test-user"}
    if email is not None:
        claims["email"] = email
    claims |= extra or {}
    for name in omit:
        claims.pop(name, None)
    signing_key: Any = None if alg == "none" else key if key is not None else TEST_PRIVATE_KEY
    return jwt.encode(claims, signing_key, algorithm=alg, headers={"kid": kid})


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
