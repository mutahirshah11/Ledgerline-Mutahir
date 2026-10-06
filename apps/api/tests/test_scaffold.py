"""US1 scaffold: the API object exists, has no product routes yet, and the pack is unchanged."""

import hashlib
import json
from pathlib import Path

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
