"""Token verification and roles (F-12.R3, F-12.R4, contracts/auth.md, research R1, C10).

Supabase access tokens are verified locally with PyJWT against the project's JWKS (`ES256` or
`RS256` only). Token expiry is checked by PyJWT against real time with 30 s leeway (tokens are
issued in real time). The JWKS cache measures its age with a monotonic clock, not the scenario
clock: it is process plumbing, not a business timestamp. Tokens and the `Authorization` header
are never logged.
"""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Any, Literal
from uuid import UUID

import httpx
import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings, get_settings
from app.core.db import Repo, get_repo
from app.core.errors import ApiError, UpstreamFailed
from app.domain.enums import ErrorCode, Role, RunStatus
from app.domain.models import AllowedUser

logger = logging.getLogger("ledgerline.auth")

ALGORITHMS = ("ES256", "RS256")
AUDIENCE = "authenticated"
LEEWAY_S = 30
JWKS_TIMEOUT_S = 5.0
JWKS_ATTEMPTS = 2
SERVICE = "supabase-auth"

JwksFetcher = Callable[[str], Any]
"""Returns the JWKS document (`{"keys": [...]}`) at a URL; raises UpstreamFailed."""


def http_jwks_fetcher(url: str, *, client: httpx.Client | None = None) -> Any:
    """GET the JWKS: 5 s timeout, 2 attempts, then UpstreamFailed (FM-14, 502)."""
    http = client or httpx.Client(timeout=JWKS_TIMEOUT_S)
    detail = "no attempt"
    try:
        for _ in range(JWKS_ATTEMPTS):
            try:
                resp = http.get(url, timeout=JWKS_TIMEOUT_S)
                resp.raise_for_status()
                return resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                detail = type(exc).__name__
    finally:
        if client is None:
            http.close()
    raise UpstreamFailed(SERVICE, f"JWKS fetch failed ({detail})", attempts=JWKS_ATTEMPTS)


def _parse_jwks(doc: Any) -> dict[str, jwt.PyJWK]:
    """Parsed keys by `kid`. A malformed document is UpstreamFailed; a key PyJWT cannot use
    (another key type or purpose) is skipped."""
    keys = doc.get("keys") if isinstance(doc, dict) else None
    if not isinstance(keys, list) or not all(isinstance(k, dict) for k in keys):
        raise UpstreamFailed(SERVICE, "JWKS document is malformed")
    parsed: dict[str, jwt.PyJWK] = {}
    for jwk in keys:
        kid = jwk.get("kid")
        if not isinstance(kid, str):
            raise UpstreamFailed(SERVICE, "JWKS key without a string kid")
        try:
            parsed[kid] = jwt.PyJWK(jwk)
        except jwt.PyJWTError as exc:
            logger.warning("JWKS key %s skipped: %s", kid, type(exc).__name__)
    return parsed


@dataclass(frozen=True)
class _Entry:
    at: float
    keys: dict[str, jwt.PyJWK]


_Need = Literal["missing", "kid"] | None


class JwksCache:
    """Parsed JWKS keys by `kid`, kept `ttl_s` seconds (default `JWKS_CACHE_S`).

    An unknown `kid` triggers a refetch, and a failed fetch is remembered; either blocks further
    forced refetches for `min_refetch_s` (default `JWKS_MIN_REFETCH_S`), so bad kids cannot
    flood Supabase and an outage costs one fetch per interval (502 meanwhile). Fetches run
    outside the state lock, one at a time, re-checking the cache once they hold the fetch lock.
    """

    def __init__(
        self,
        fetcher: JwksFetcher,
        *,
        ttl_s: int | None = None,
        min_refetch_s: int | None = None,
        mono: Callable[[], float] = time.monotonic,
    ) -> None:
        settings = get_settings()
        self._fetcher = fetcher
        self._ttl = float(ttl_s if ttl_s is not None else settings.JWKS_CACHE_S)
        self._min_refetch = float(
            min_refetch_s if min_refetch_s is not None else settings.JWKS_MIN_REFETCH_S
        )
        self._mono = mono
        self._entries: dict[str, _Entry] = {}
        self._forced_at: dict[str, float] = {}
        self._lock = threading.Lock()
        self._fetch_lock = threading.Lock()

    def _lookup(self, url: str, kid: str) -> tuple[jwt.PyJWK | None, _Need]:
        """The key and whether a fetch is needed; UpstreamFailed while a failure is recent."""
        with self._lock:
            now = self._mono()
            entry = self._entries.get(url)
            forced = self._forced_at.get(url)
            throttled = forced is not None and now - forced < self._min_refetch
            if entry is not None and now - entry.at < self._ttl:
                key = entry.keys.get(kid)
                if key is not None or throttled:
                    return key, None
                return None, "kid"
            if throttled:  # no usable keys and the last forced fetch was recent: it failed
                raise UpstreamFailed(SERVICE, "JWKS unavailable, retrying later")
            return None, "missing"

    def get_key(self, url: str, kid: str) -> jwt.PyJWK | None:
        """The parsed key with `kid` from the JWKS at `url`, or None."""
        key, need = self._lookup(url, kid)
        if need is None:
            return key
        with self._fetch_lock:
            key, need = self._lookup(url, kid)  # another request may have fetched meanwhile
            if need is None:
                return key
            if need == "kid":
                with self._lock:
                    self._forced_at[url] = self._mono()
            try:
                keys = _parse_jwks(self._fetcher(url))
            except Exception:
                with self._lock:
                    self._forced_at[url] = self._mono()
                raise
            with self._lock:
                self._entries[url] = _Entry(self._mono(), keys)
            return keys.get(kid)


@lru_cache(maxsize=1)
def get_jwks_cache() -> JwksCache:
    """FastAPI dependency: the process-wide JWKS cache over HTTP; tests override it."""
    return JwksCache(http_jwks_fetcher)


def _unauthenticated() -> ApiError:
    return ApiError(ErrorCode.UNAUTHENTICATED, 401, "Sign in again")


def verify_token(token: str, jwks: JwksCache, settings: Settings) -> str:
    """The token's email, lower-cased; ApiError 401 on any failed check (contracts/auth.md
    checks 2 to 5). UpstreamFailed when `SUPABASE_URL` is unset or the JWKS cannot be fetched."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise _unauthenticated() from None
    alg, kid = header.get("alg"), header.get("kid")
    if alg not in ALGORITHMS or not isinstance(kid, str):
        raise _unauthenticated()
    base = settings.SUPABASE_URL.rstrip("/")
    if not base:
        raise UpstreamFailed(SERVICE, "SUPABASE_URL not set")
    issuer = f"{base}/auth/v1"
    key = jwks.get_key(f"{issuer}/.well-known/jwks.json", kid)
    if key is None or key.algorithm_name != alg:  # the JWK's (declared or implied) alg rules
        raise _unauthenticated()
    try:
        claims = jwt.decode(
            token,
            key.key,
            algorithms=[alg],
            audience=AUDIENCE,
            issuer=issuer,
            leeway=LEEWAY_S,
            options={"require": ["exp", "iss", "aud"]},
        )
    except (jwt.PyJWTError, ValueError, TypeError):
        raise _unauthenticated() from None
    email = claims.get("email")
    if not isinstance(email, str) or not email:
        raise _unauthenticated()
    return email.lower()


_bearer = HTTPBearer(auto_error=False)


def current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    repo: Annotated[Repo, Depends(get_repo)],
    jwks: Annotated[JwksCache, Depends(get_jwks_cache)],
) -> AllowedUser:
    """FastAPI dependency: the allow-listed caller (checks 1 to 6); 401 or 403 otherwise."""
    if credentials is None or not credentials.credentials:
        raise _unauthenticated()
    email = verify_token(credentials.credentials, jwks, get_settings())
    user = repo.get_allowed_user(email)
    if user is None:
        raise ApiError(ErrorCode.NOT_ALLOWED, 403, "Your email is not on the list for this app")
    return user


def _run_failed(request: Request, repo: Repo) -> bool:
    raw = request.path_params.get("id")
    try:
        run_id = UUID(str(raw))
    except ValueError:
        return False
    return repo.get_run(run_id).status is RunStatus.FAILED


class RoleGuard:
    """FastAPI dependency (check 7, F-12.R4): the caller's role must be in `allowed`. With
    `ops_retry`, OPS also passes when the run in the `{id}` path parameter is FAILED. This is a
    pre-check only: stage services enforce the retry atomically through the `expected` status
    set of `transition_run`. `allowed` and `ops_retry` are public so tests can audit routes."""

    def __init__(self, roles: tuple[Role, ...], ops_retry: bool) -> None:
        self.allowed = frozenset(roles)
        self.ops_retry = ops_retry

    def __call__(
        self,
        request: Request,
        user: Annotated[AllowedUser, Depends(current_user)],
        repo: Annotated[Repo, Depends(get_repo)],
    ) -> AllowedUser:
        if user.role in self.allowed:
            return user
        if self.ops_retry and user.role is Role.OPS and _run_failed(request, repo):
            return user
        raise ApiError(ErrorCode.NOT_ALLOWED, 403, "Your role does not allow this")


def require_role(*roles: Role, ops_retry: bool = False) -> RoleGuard:
    """The `RoleGuard` for `roles`; use as `Depends(require_role(Role.CONTROLLER))`."""
    return RoleGuard(roles, ops_retry)
