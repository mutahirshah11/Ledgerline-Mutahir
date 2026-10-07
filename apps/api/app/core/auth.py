"""Token verification and roles (F-12.R3, F-12.R4, contracts/auth.md, research R1, C10).

Supabase access tokens are verified locally with PyJWT against the project's JWKS (`ES256` or
`RS256` only). Token expiry is checked by PyJWT against real time with 30 s leeway (tokens are
issued in real time); the JWKS cache age is measured on the scenario clock (`app.core.clock`),
so under a frozen clock keys are refreshed only on an unknown `kid`. Tokens and the
`Authorization` header are never logged.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Annotated, Any
from uuid import UUID

import httpx
import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core import clock
from app.core.config import Settings, get_settings
from app.core.db import Repo, get_repo
from app.core.errors import ApiError, UpstreamFailed
from app.domain.enums import ErrorCode, Role, RunStatus
from app.domain.models import AllowedUser

ALGORITHMS = ("ES256", "RS256")
AUDIENCE = "authenticated"
LEEWAY_S = 30
JWKS_TIMEOUT_S = 5.0
JWKS_ATTEMPTS = 2
SERVICE = "supabase-auth"

JwksFetcher = Callable[[str], dict[str, Any]]
"""Returns the JWKS document (`{"keys": [...]}`) at a URL; raises UpstreamFailed."""


def http_jwks_fetcher(url: str, *, client: httpx.Client | None = None) -> dict[str, Any]:
    """GET the JWKS: 5 s timeout, 2 attempts, then UpstreamFailed (FM-14, 502)."""
    http = client or httpx.Client(timeout=JWKS_TIMEOUT_S)
    detail = "no attempt"
    try:
        for _ in range(JWKS_ATTEMPTS):
            try:
                resp = http.get(url, timeout=JWKS_TIMEOUT_S)
                resp.raise_for_status()
                doc: dict[str, Any] = resp.json()
                return doc
            except (httpx.HTTPError, ValueError) as exc:
                detail = type(exc).__name__
    finally:
        if client is None:
            http.close()
    raise UpstreamFailed(SERVICE, f"JWKS fetch failed ({detail})", attempts=JWKS_ATTEMPTS)


@dataclass(frozen=True)
class _Entry:
    at: datetime
    keys: dict[str, dict[str, Any]]


class JwksCache:
    """JWKS keys by `kid`, kept `ttl_s` seconds (default `JWKS_CACHE_S`); an unknown `kid`
    triggers one refetch."""

    def __init__(
        self,
        fetcher: JwksFetcher,
        *,
        ttl_s: int | None = None,
        now: Callable[[], datetime] = clock.now,
    ) -> None:
        self._fetcher = fetcher
        self._ttl = timedelta(seconds=ttl_s if ttl_s is not None else get_settings().JWKS_CACHE_S)
        self._now = now
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def _fetch(self, url: str) -> _Entry:
        doc = self._fetcher(url)
        keys = {k["kid"]: k for k in doc.get("keys", []) if isinstance(k, dict) and "kid" in k}
        entry = _Entry(self._now(), keys)
        self._entries[url] = entry
        return entry

    def get_key(self, url: str, kid: str) -> dict[str, Any] | None:
        """The JWK with `kid` from the JWKS at `url`, or None."""
        with self._lock:
            entry = self._entries.get(url)
            fetched = entry is None or self._now() - entry.at >= self._ttl
            if entry is None or fetched:
                entry = self._fetch(url)
            key = entry.keys.get(kid)
            if key is None and not fetched:
                key = self._fetch(url).keys.get(kid)
            return key


@lru_cache(maxsize=1)
def get_jwks_cache() -> JwksCache:
    """FastAPI dependency: the process-wide JWKS cache over HTTP; tests override it."""
    return JwksCache(http_jwks_fetcher)


def _unauthenticated() -> ApiError:
    return ApiError(ErrorCode.UNAUTHENTICATED, 401, "Sign in again")


def verify_token(token: str, jwks: JwksCache, settings: Settings) -> str:
    """The token's email, lower-cased; ApiError 401 on any failed check (contracts/auth.md
    checks 2 to 5). UpstreamFailed passes through when the JWKS cannot be fetched."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise _unauthenticated() from None
    alg, kid = header.get("alg"), header.get("kid")
    if alg not in ALGORITHMS or not isinstance(kid, str):
        raise _unauthenticated()
    issuer = f"{settings.SUPABASE_URL}/auth/v1"
    jwk = jwks.get_key(f"{issuer}/.well-known/jwks.json", kid)
    if jwk is None:
        raise _unauthenticated()
    try:
        key = jwt.PyJWK(jwk, algorithm=alg)
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


def require_role(*roles: Role, ops_retry: bool = False) -> Callable[..., AllowedUser]:
    """FastAPI dependency factory (check 7, F-12.R4): the caller's role must be in `roles`.
    With `ops_retry`, OPS also passes when the run in the `{id}` path parameter is FAILED."""
    allowed = frozenset(roles)

    def guard(
        request: Request,
        user: Annotated[AllowedUser, Depends(current_user)],
        repo: Annotated[Repo, Depends(get_repo)],
    ) -> AllowedUser:
        if user.role in allowed:
            return user
        if ops_retry and user.role is Role.OPS and _run_failed(request, repo):
            return user
        raise ApiError(ErrorCode.NOT_ALLOWED, 403, "Your role does not allow this")

    return guard
