"""US1 scaffold: the API object exists, has no product routes yet, and the pack is unchanged."""

import hashlib
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

FASTAPI_BUILTIN_PATHS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def test_app_object_is_fastapi() -> None:
    from app.main import app

    assert isinstance(app, FastAPI)


def test_app_has_no_product_routes() -> None:
    from app.main import app

    paths = {getattr(r, "path", "") for r in app.routes}
    assert paths <= FASTAPI_BUILTIN_PATHS
    assert not [r for r in app.routes if isinstance(r, APIRoute)]


def test_pack_files_unchanged(pack_dir: Path) -> None:
    expected = json.loads((Path(__file__).parent / "pack_hashes.json").read_text())
    actual = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(pack_dir.iterdir())
    }
    assert actual == expected


def test_docs_routes_disabled_by_default() -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app

    client = TestClient(create_app())
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_docs_routes_enabled_when_faults_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from app.core.config import get_settings
    from app.main import create_app

    monkeypatch.setenv("ALLOW_FAULTS", "true")
    get_settings.cache_clear()
    client = TestClient(create_app())
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200


def test_cors_allows_only_configured_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from app.core.config import get_settings
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
    from fastapi import APIRouter, Depends, HTTPException
    from fastapi.testclient import TestClient

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
    app.include_router(pub)
    app.include_router(prot)
    client = TestClient(app)
    assert client.get("/api/v1/ping").status_code == 200
    assert client.get("/api/v1/secret").status_code == 401
