"""F-12 access and auth (004 T022, contracts/auth.md, research R1).

Endpoints of later stories do not exist yet, so the role matrix runs on stub routes guarded by
the real `require_role` (mounted under `/test`); AC1 iterates the real route table, so routes
added later are covered automatically.
"""

import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient

from app.core.auth import JwksCache, http_jwks_fetcher, require_role
from app.core.errors import UpstreamFailed
from app.core.memory_repo import MemoryRepo
from app.domain.enums import Role, RunStatus, Stage
from tests.conftest import SEEDED_USERS
from tests.factories import make_run
from tests.fakes.api import TEST_ISS, FakeJwksFetcher, make_token, public_jwk

CTRL, REV, OPS = Role.CONTROLLER, Role.REVIEWER, Role.OPS

OTHER_KEY = ec.generate_private_key(ec.SECP256R1())


def _error(resp: Any) -> dict[str, Any]:
    body: dict[str, Any] = resp.json()["error"]
    return body


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- AC1, AC2 ----------------------------------------------------------------------------


def test_F12_AC1_every_endpoint_requires_token(client: TestClient) -> None:
    """The route table comes from the OpenAPI schema: FastAPI mounts included routers lazily,
    so `app.routes` does not list them."""
    paths: dict[str, dict[str, Any]] = client.app.openapi()["paths"]  # type: ignore[attr-defined]
    routes = {p: ops for p, ops in paths.items() if p != "/api/v1/health"}
    assert "/api/v1/me" in routes
    assert all(p.startswith("/api/v1/") for p in routes)
    for template, ops in routes.items():
        path = template.replace("{page_no}", "1")
        path = re.sub(r"\{[^}]+\}", str(uuid4()), path)
        for method in ops:
            resp = client.request(method.upper(), path)
            assert resp.status_code == 401, (method, template)
            assert _error(resp)["code"] == "UNAUTHENTICATED"


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
    ],
)
def test_F12_R3_rejects_bad_tokens(client: TestClient, token: Any) -> None:
    resp = client.get("/api/v1/me", headers=_bearer(token()))
    assert resp.status_code == 401
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
    unknown = make_token(SEEDED_USERS["controller"][0], kid="never")
    assert client.get("/api/v1/me", headers=_bearer(unknown)).status_code == 401
    assert len(jwks_fetcher.calls) == 3


def test_F12_R3_jwks_cache_expires_after_ttl() -> None:
    t = [datetime(2026, 10, 15, tzinfo=UTC)]
    fetcher = FakeJwksFetcher()
    cache = JwksCache(fetcher, ttl_s=600, now=lambda: t[0])
    url = f"{TEST_ISS}/.well-known/jwks.json"
    kid = public_jwk()["kid"]
    assert cache.get_key(url, kid) is not None
    t[0] += timedelta(seconds=599)
    assert cache.get_key(url, kid) is not None
    assert len(fetcher.calls) == 1
    t[0] += timedelta(seconds=2)
    assert cache.get_key(url, kid) is not None
    assert len(fetcher.calls) == 2


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


# --- EP-02 -------------------------------------------------------------------------------


def test_F12_EP02_me_returns_profile(client: TestClient, tokens: dict[str, dict[str, str]]) -> None:
    resp = client.get("/api/v1/me", headers=tokens["reviewer"])
    assert resp.status_code == 200
    assert resp.json() == {
        "email": "reviewer@kestrel.example",
        "display_name": "Reviewer (Demo)",
        "role": "REVIEWER",
    }
