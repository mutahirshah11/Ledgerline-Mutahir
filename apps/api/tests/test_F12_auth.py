"""F-12 access and auth (004 T022, contracts/auth.md, research R1).

Endpoints of later stories do not exist yet, so the role matrix runs on stub routes guarded by
the real `require_role` (mounted under `/test`); AC1 iterates the real route table, so routes
added later are covered automatically.
"""

import re
import secrets
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

from app.core.auth import JwksCache, RoleGuard, current_user, http_jwks_fetcher, require_role
from app.core.config import get_settings
from app.core.errors import UpstreamFailed
from app.core.memory_repo import MemoryRepo
from app.domain.enums import Role, RunStatus, Stage
from tests.conftest import SEEDED_USERS
from tests.factories import make_run
from tests.fakes.api import TEST_ISS, TEST_KID, FakeJwksFetcher, make_token, public_jwk
from tests.support.routes import api_routes

CTRL, REV, OPS = Role.CONTROLLER, Role.REVIEWER, Role.OPS

OTHER_KEY = ec.generate_private_key(ec.SECP256R1())
RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
CONTROLLER_EMAIL = SEEDED_USERS["controller"][0]
JWKS_URL = f"{TEST_ISS}/.well-known/jwks.json"


def rsa_jwk(kid: str, alg: str | None = "RS256") -> dict[str, Any]:
    jwk: dict[str, Any] = RSAAlgorithm.to_jwk(RSA_KEY.public_key(), as_dict=True)
    return jwk | {"kid": kid, "use": "sig"} | ({"alg": alg} if alg else {})


def _error(resp: Any) -> dict[str, Any]:
    body: dict[str, Any] = resp.json()["error"]
    return body


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- AC1, AC2 ----------------------------------------------------------------------------


def test_F12_AC1_every_endpoint_requires_token(client: TestClient) -> None:
    app: FastAPI = client.app  # type: ignore[assignment]
    routes = [r for r in api_routes(app) if r.path != "/api/v1/health"]
    paths = {r.path for r in routes}
    assert routes and "/api/v1/me" in paths
    assert set(app.openapi()["paths"]) - {"/api/v1/health"} <= paths
    assert all(p.startswith("/api/v1/") for p in paths)
    for route in routes:
        path = route.path.replace("{page_no}", "1")
        path = re.sub(r"\{[^}]+\}", str(uuid4()), path)
        for method in sorted(route.methods):
            resp = client.request(method, path)
            assert resp.status_code == 401, (method, route.path)
            assert _error(resp)["code"] == "UNAUTHENTICATED"
            assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_F12_AC2_unlisted_email_forbidden(
    client: TestClient, tokens: dict[str, dict[str, str]]
) -> None:
    resp = client.get("/api/v1/me", headers=tokens["stranger"])
    assert resp.status_code == 403
    assert _error(resp)["code"] == "NOT_ALLOWED"


# --- R3 token verification ---------------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(
            lambda: make_token(SEEDED_USERS["controller"][0], key=OTHER_KEY), id="bad-sig"
        ),
        pytest.param(
            lambda: make_token(SEEDED_USERS["controller"][0], exp=datetime(2000, 1, 1, tzinfo=UTC)),
            id="expired",
        ),
        pytest.param(
            lambda: make_token(SEEDED_USERS["controller"][0], iss="https://x.supabase.co/auth/v1"),
            id="wrong-iss",
        ),
        pytest.param(lambda: make_token(SEEDED_USERS["controller"][0], aud="anon"), id="wrong-aud"),
        pytest.param(
            lambda: make_token(
                SEEDED_USERS["controller"][0], alg="HS256", key=secrets.token_bytes(32)
            ),
            id="alg-HS256",
        ),
        pytest.param(lambda: make_token(None), id="missing-email"),
        pytest.param(
            lambda: make_token(SEEDED_USERS["controller"][0], kid="nope"), id="unknown-kid"
        ),
        pytest.param(lambda: "not-a-jwt", id="garbage"),
        pytest.param(lambda: make_token(CONTROLLER_EMAIL, alg="none"), id="alg-none"),
        pytest.param(
            lambda: make_token(CONTROLLER_EMAIL, alg="RS256", key=RSA_KEY, kid=TEST_KID),
            id="RS256-header-EC-kid",
        ),
        pytest.param(lambda: make_token(CONTROLLER_EMAIL, omit=("exp",)), id="no-exp"),
        pytest.param(
            # the JWK for this kid declares RS256; the header says ES256
            lambda: make_token(CONTROLLER_EMAIL, kid="rsa-declared"),
            id="jwk-alg-differs",
        ),
    ],
)
def test_F12_R3_rejects_bad_tokens(
    client: TestClient, jwks_fetcher: FakeJwksFetcher, token: Any
) -> None:
    jwks_fetcher.keys = [public_jwk(), rsa_jwk("rsa"), rsa_jwk("rsa-declared")]
    resp = client.get("/api/v1/me", headers=_bearer(token()))
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"
    assert _error(resp) == {
        "code": "UNAUTHENTICATED",
        "message": "Sign in again",
        "stage": None,
        "details": {},
    }


def test_F12_R3_non_bearer_header_401(client: TestClient) -> None:
    resp = client.get("/api/v1/me", headers={"Authorization": "Basic abc"})
    assert resp.status_code == 401
    assert _error(resp)["code"] == "UNAUTHENTICATED"


def test_F12_R3_accepts_rs256(client: TestClient, jwks_fetcher: FakeJwksFetcher) -> None:
    jwks_fetcher.keys = [public_jwk(), rsa_jwk("rsa")]
    token = make_token(CONTROLLER_EMAIL, alg="RS256", key=RSA_KEY, kid="rsa")
    resp = client.get("/api/v1/me", headers=_bearer(token))
    assert resp.status_code == 200
    assert resp.json()["role"] == "CONTROLLER"


def test_F12_R3_supabase_url_trailing_slash(
    client: TestClient, tokens: dict[str, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SUPABASE_URL", TEST_ISS.removesuffix("/auth/v1") + "/")
    get_settings.cache_clear()
    assert client.get("/api/v1/me", headers=tokens["controller"]).status_code == 200


def test_F12_R3_supabase_url_unset_502(
    client: TestClient, tokens: dict[str, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SUPABASE_URL")
    get_settings.cache_clear()
    resp = client.get("/api/v1/me", headers=tokens["controller"])
    assert resp.status_code == 502
    assert _error(resp)["details"]["service"] == "supabase-auth"


def test_F12_R3_email_case_insensitive(client: TestClient) -> None:
    resp = client.get("/api/v1/me", headers=_bearer(make_token("Controller@Kestrel.EXAMPLE")))
    assert resp.status_code == 200
    assert resp.json()["email"] == "controller@kestrel.example"


def test_F12_R3_jwks_unreachable_502(
    client: TestClient, tokens: dict[str, dict[str, str]], jwks_fetcher: FakeJwksFetcher
) -> None:
    jwks_fetcher.fail = UpstreamFailed("supabase-auth", "unreachable")
    resp = client.get("/api/v1/me", headers=tokens["controller"])
    assert resp.status_code == 502
    assert _error(resp)["code"] == "UPSTREAM_FAILED"


def test_F12_R3_unknown_kid_refetches_once(
    client: TestClient, tokens: dict[str, dict[str, str]], jwks_fetcher: FakeJwksFetcher
) -> None:
    assert client.get("/api/v1/me", headers=tokens["controller"]).status_code == 200
    assert len(jwks_fetcher.calls) == 1
    # Supabase rotates keys: the new kid is unknown to the cache, so it refetches once.
    jwks_fetcher.keys = [public_jwk(OTHER_KEY, "rotated")]
    rotated = make_token(SEEDED_USERS["controller"][0], key=OTHER_KEY, kid="rotated")
    assert client.get("/api/v1/me", headers=_bearer(rotated)).status_code == 200
    assert len(jwks_fetcher.calls) == 2
    assert client.get("/api/v1/me", headers=_bearer(rotated)).status_code == 200
    assert len(jwks_fetcher.calls) == 2
    # a second unknown kid inside JWKS_MIN_REFETCH_S does not refetch (no DoS by bad kids)
    unknown = make_token(SEEDED_USERS["controller"][0], kid="never")
    assert client.get("/api/v1/me", headers=_bearer(unknown)).status_code == 401
    assert len(jwks_fetcher.calls) == 2


class Mono:
    """A monotonic-clock stand-in the tests advance by hand."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_F12_R3_jwks_cache_expires_after_ttl() -> None:
    mono = Mono()
    fetcher = FakeJwksFetcher()
    cache = JwksCache(fetcher, ttl_s=600, min_refetch_s=30, mono=mono)
    assert cache.get_key(JWKS_URL, TEST_KID) is not None
    mono.t += 599
    assert cache.get_key(JWKS_URL, TEST_KID) is not None
    assert len(fetcher.calls) == 1
    mono.t += 2
    assert cache.get_key(JWKS_URL, TEST_KID) is not None
    assert len(fetcher.calls) == 2


def test_F12_R3_unknown_kids_refetch_at_most_once_per_interval() -> None:
    mono = Mono()
    fetcher = FakeJwksFetcher()
    cache = JwksCache(fetcher, ttl_s=600, min_refetch_s=30, mono=mono)
    assert cache.get_key(JWKS_URL, TEST_KID) is not None
    assert cache.get_key(JWKS_URL, "unknown-a") is None
    assert cache.get_key(JWKS_URL, "unknown-b") is None
    assert len(fetcher.calls) == 2
    mono.t += 29
    assert cache.get_key(JWKS_URL, "unknown-c") is None
    assert len(fetcher.calls) == 2
    mono.t += 2
    assert cache.get_key(JWKS_URL, "unknown-d") is None
    assert len(fetcher.calls) == 3
    assert cache.get_key(JWKS_URL, TEST_KID) is not None
    assert len(fetcher.calls) == 3


def test_F12_R3_outage_costs_one_fetch_per_interval() -> None:
    mono = Mono()
    fetcher = FakeJwksFetcher(fail=UpstreamFailed("supabase-auth", "unreachable"))
    cache = JwksCache(fetcher, ttl_s=600, min_refetch_s=30, mono=mono)
    for _ in range(3):
        with pytest.raises(UpstreamFailed):
            cache.get_key(JWKS_URL, TEST_KID)
    assert len(fetcher.calls) == 1
    mono.t += 30
    fetcher.fail = None
    assert cache.get_key(JWKS_URL, TEST_KID) is not None
    assert len(fetcher.calls) == 2


@pytest.mark.parametrize(
    "doc",
    [
        pytest.param(["not", "a", "dict"], id="not-a-dict"),
        pytest.param({"keys": "nope"}, id="keys-not-a-list"),
        pytest.param({"keys": [public_jwk() | {"kid": 7}]}, id="kid-not-a-str"),
        pytest.param(
            {"keys": [{k: v for k, v in public_jwk().items() if k != "kid"}]}, id="no-kid"
        ),
    ],
)
def test_F12_R3_bad_jwks_document_upstream_failed(doc: Any) -> None:
    cache = JwksCache(lambda url: doc, ttl_s=600, min_refetch_s=30, mono=Mono())
    with pytest.raises(UpstreamFailed) as info:
        cache.get_key(JWKS_URL, TEST_KID)
    assert info.value.service == "supabase-auth"


def test_F12_R3_http_jwks_fetch_failure_upstream_failed() -> None:
    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(UpstreamFailed) as info:
            http_jwks_fetcher("https://x.supabase.co/auth/v1/.well-known/jwks.json", client=http)
    assert info.value.service == "supabase-auth"
    assert len(attempts) == 2


def test_F12_R3_http_jwks_fetch_returns_keys() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"keys": [public_jwk()]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        assert http_jwks_fetcher("https://x/jwks", client=http) == {"keys": [public_jwk()]}


# --- R4 roles (contracts/auth.md matrix, Phase 3 rows) ----------------------------------

ROLE_MATRIX: list[tuple[str, str, str, tuple[Role, ...], bool]] = [
    ("EP-03", "POST", "/runs", (CTRL,), False),
    ("EP-04", "GET", "/runs", (CTRL, REV, OPS), False),
    ("EP-05", "GET", "/runs/{id}", (CTRL, REV, OPS), False),
    ("EP-06", "POST", "/runs/{id}/uploads", (CTRL,), False),
    ("EP-07", "POST", "/runs/{id}/uploads/confirm", (CTRL,), False),
    ("EP-08", "POST", "/runs/{id}/validate", (CTRL,), True),
    ("EP-09", "POST", "/runs/{id}/pages/{page_no}/extract", (CTRL,), True),
    ("EP-10", "POST", "/runs/{id}/reconcile", (CTRL,), True),
    ("EP-19", "GET", "/runs/{id}/events", (CTRL, REV, OPS), False),
    ("EP-20", "GET", "/runs/{id}/audit", (CTRL, REV, OPS), False),
    ("EP-21", "GET", "/runs/{id}/normalisation-log", (CTRL, REV, OPS), False),
]


def _stub_routes(client: TestClient) -> None:
    """Mount each matrix row under `/test`, guarded by `require_role` as the row says."""
    router = APIRouter(prefix="/test")
    for _ep, method, path, roles, ops_retry in ROLE_MATRIX:
        router.add_api_route(
            path,
            lambda: {"ok": True},
            methods=[method],
            dependencies=[Depends(require_role(*roles, ops_retry=ops_retry))],
        )
    client.app.include_router(router)  # type: ignore[attr-defined]


def _url(path: str, run_id: object) -> str:
    return "/test" + path.replace("{id}", str(run_id)).replace("{page_no}", "1")


def test_F12_R4_role_guard(client: TestClient, tokens: dict[str, dict[str, str]]) -> None:
    """REVIEWER may not create a run (EP-03); F-12 AC3 on EP-17 is Phase 4."""
    _stub_routes(client)
    resp = client.post("/test/runs", headers=tokens["reviewer"])
    assert resp.status_code == 403
    assert _error(resp)["code"] == "NOT_ALLOWED"


@pytest.mark.parametrize("who", ["controller", "reviewer", "ops"])
@pytest.mark.parametrize("row", ROLE_MATRIX, ids=[r[0] for r in ROLE_MATRIX])
def test_F12_R4_role_matrix(
    client: TestClient,
    tokens: dict[str, dict[str, str]],
    memory_repo: MemoryRepo,
    row: tuple[str, str, str, tuple[Role, ...], bool],
    who: str,
) -> None:
    _ep, method, path, roles, _ops_retry = row
    _stub_routes(client)
    run = memory_repo.create_run(make_run())  # CREATED: not a retry
    resp = client.request(method, _url(path, run.id), headers=tokens[who])
    allowed = Role(SEEDED_USERS[who][2]) in roles
    assert resp.status_code == (200 if allowed else 403)
    if not allowed:
        assert _error(resp)["code"] == "NOT_ALLOWED"


def test_F12_R4_matrix_me_all_roles(client: TestClient, tokens: dict[str, dict[str, str]]) -> None:
    for who in ("controller", "reviewer", "ops"):
        assert client.get("/api/v1/me", headers=tokens[who]).status_code == 200


def test_F12_R4_ops_retry_only_when_failed(
    client: TestClient, tokens: dict[str, dict[str, str]], memory_repo: MemoryRepo
) -> None:
    _stub_routes(client)
    run = memory_repo.create_run(make_run())
    retry_paths = [r[2] for r in ROLE_MATRIX if r[4]]
    for path in retry_paths:
        assert client.post(_url(path, run.id), headers=tokens["ops"]).status_code == 403
        assert client.post(_url(path, run.id), headers=tokens["reviewer"]).status_code == 403
    memory_repo.transition_run(
        run.id, RunStatus.FAILED, expected={RunStatus.CREATED}, failed_stage=Stage.VALIDATE
    )
    for path in retry_paths:
        assert client.post(_url(path, run.id), headers=tokens["ops"]).status_code == 200
        assert client.post(_url(path, run.id), headers=tokens["reviewer"]).status_code == 403


def test_F12_R4_ops_retry_unknown_run_404(
    client: TestClient, tokens: dict[str, dict[str, str]]
) -> None:
    _stub_routes(client)
    resp = client.post(_url("/runs/{id}/validate", uuid4()), headers=tokens["ops"])
    assert resp.status_code == 404


ALL_ROLES = frozenset(Role)

STATIC_MATRIX: dict[tuple[str, str], tuple[frozenset[Role], bool]] = {
    ("GET", "/me"): (ALL_ROLES, False),
    **{(m, p): (frozenset(roles), retry) for _ep, m, p, roles, retry in ROLE_MATRIX},
    # Phase 4 rows (contracts/auth.md), checked as soon as the routes exist
    ("GET", "/runs/{id}/groups"): (ALL_ROLES, False),
    ("GET", "/runs/{id}/flags"): (ALL_ROLES, False),
    ("GET", "/runs/{id}/proof"): (ALL_ROLES, False),
    ("GET", "/runs/{id}/review-items"): (ALL_ROLES, False),
    ("POST", "/review-items/{id}/resolve"): (frozenset({CTRL, REV}), False),
    ("GET", "/runs/{id}/pages/{page_no}/image"): (ALL_ROLES, False),
    ("POST", "/runs/{id}/finalise"): (frozenset({CTRL}), False),
    ("GET", "/runs/{id}/report"): (ALL_ROLES, False),
}
"""(method, path under /api/v1) -> (roles allowed, OPS retry when FAILED); EP-01 has no token."""


def _shape(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


def guard_problems(app: FastAPI) -> list[str]:
    """Every /api/v1 route must be in the matrix, depend on `current_user`, and carry the
    `require_role` guard its row asks for (none needed when all roles are allowed)."""
    matrix = {(m, "/api/v1" + _shape(p)): row for (m, p), row in STATIC_MATRIX.items()}
    problems: list[str] = []
    for route in api_routes(app):
        if route.path == "/api/v1/health":
            continue
        calls = list(route.calls())
        guards = {(c.allowed, c.ops_retry) for c in calls if isinstance(c, RoleGuard)}
        for method in sorted(route.methods):
            row = matrix.get((method, _shape(route.path)))
            where = f"{method} {route.path}"
            if row is None:
                problems.append(f"{where}: not in the roles matrix")
                continue
            if current_user not in calls:
                problems.append(f"{where}: no current_user")
            if row == (ALL_ROLES, False) and guards <= {row}:
                continue
            if guards != {row}:
                problems.append(f"{where}: guard {guards} != {row}")
    return problems


def test_F12_R4_routes_carry_matrix_guards(client: TestClient) -> None:
    app: FastAPI = client.app  # type: ignore[assignment]
    assert guard_problems(app) == []


def test_F12_R4_guard_check_catches_missing_guard() -> None:
    app = FastAPI()
    router = APIRouter(prefix="/api/v1")
    router.add_api_route("/runs", lambda: {}, methods=["POST"])  # EP-03 with no guard
    router.add_api_route(
        "/runs/{id}/validate",
        lambda: {},
        methods=["POST"],
        dependencies=[Depends(require_role(CTRL))],  # OPS retry missing
    )
    router.add_api_route("/unlisted", lambda: {}, methods=["GET"])
    app.include_router(router)
    problems = guard_problems(app)
    assert any(p.startswith("POST /api/v1/runs: no current_user") for p in problems)
    assert any(p.startswith("POST /api/v1/runs: guard") for p in problems)
    assert any(p.startswith("POST /api/v1/runs/{id}/validate: guard") for p in problems)
    assert any(p.startswith("GET /api/v1/unlisted: not in") for p in problems)


def test_F12_R4_require_role_exposes_its_rule() -> None:
    guard = require_role(CTRL, ops_retry=True)
    assert (guard.allowed, guard.ops_retry) == (frozenset({CTRL}), True)


# --- EP-02 -------------------------------------------------------------------------------


def test_F12_EP02_me_returns_profile(client: TestClient, tokens: dict[str, dict[str, str]]) -> None:
    resp = client.get("/api/v1/me", headers=tokens["reviewer"])
    assert resp.status_code == 200
    assert resp.json() == {
        "email": "reviewer@kestrel.example",
        "display_name": "Reviewer (Demo)",
        "role": "REVIEWER",
    }
