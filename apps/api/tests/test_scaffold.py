"""US1 scaffold: the API object exists, its routes live under /api/v1, and the pack is unchanged."""

import hashlib
import json
from pathlib import Path

import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.errors import install_error_handlers

# `app.main` is imported inside each test on purpose: importing it builds the module-level app
# from settings, which must happen after the autouse `hermetic_settings` fixture.


def test_app_object_is_fastapi() -> None:
    from app.main import app

    assert isinstance(app, FastAPI)


def test_app_routes_live_under_api_v1() -> None:
    """Phase 0 had no product routes; from Phase 3 (T025) every one is under `/api/v1`.
    Included routers are mounted lazily, so the table comes from their effective routes."""
    from app.main import API_PREFIX, app
    from tests.support.routes import api_routes

    paths = {r.path for r in api_routes(app)}
    assert paths and f"{API_PREFIX}/me" in paths
    assert set(app.openapi()["paths"]) <= paths
    assert all(p.startswith(f"{API_PREFIX}/") for p in paths)


def test_pack_files_unchanged(pack_dir: Path) -> None:
    expected = json.loads((Path(__file__).parent / "pack_hashes.json").read_text())
    actual = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(pack_dir.iterdir())
    }
    assert actual == expected


def test_docs_routes_disabled_by_default() -> None:
    from app.main import create_app

    client = TestClient(create_app())
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_docs_routes_enabled_when_faults_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import create_app

    monkeypatch.setenv("ALLOW_FAULTS", "true")
    get_settings.cache_clear()
    client = TestClient(create_app())
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200


def test_cors_allows_only_configured_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import create_app

    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.test, http://localhost:3000")
    get_settings.cache_clear()
    client = TestClient(create_app())

    def preflight(origin: str, method: str = "POST") -> tuple[int, dict[str, str]]:
        headers = {
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": "Authorization, Content-Type",
        }
        resp = client.options("/api/v1/runs", headers=headers)
        return resp.status_code, dict(resp.headers)

    status, ok = preflight("https://app.example.test")
    assert status == 200
    assert ok["access-control-allow-origin"] == "https://app.example.test"
    assert set(ok["access-control-allow-methods"].replace(" ", "").split(",")) == {"GET", "POST"}
    status, evil = preflight("https://evil.example.test")
    assert status == 400 and "access-control-allow-origin" not in evil
    assert preflight("https://app.example.test", "DELETE")[0] == 400


def test_api_routers_mount_under_api_v1() -> None:
    from app.main import API_PREFIX, build_routers

    public = APIRouter()
    protected = APIRouter()

    @public.get("/ping")
    def ping() -> dict[str, str]:
        return {"ok": "yes"}

    @protected.get("/secret")
    def secret() -> dict[str, str]:
        return {"ok": "yes"}

    def guard() -> None:
        raise HTTPException(status_code=401)

    pub, prot = build_routers(
        public_routers=[public],
        protected_routers=[protected],
        protected_dependencies=[Depends(guard)],
    )
    assert API_PREFIX == "/api/v1"
    assert prot.dependencies and not pub.dependencies
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(pub)
    app.include_router(prot)
    client = TestClient(app)
    assert client.get("/api/v1/ping").status_code == 200
    denied = client.get("/api/v1/secret")
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "UNAUTHENTICATED"
