"""FastAPI entrypoint (004 plan.md, FR-026, NFR-04, blueprint Section 10).

Route modules live in `app.routes`; each may expose `public_router` and/or `protected_router`
(plain `APIRouter`s without a prefix). `create_app()` mounts them under `/api/v1`, the protected
ones behind `PROTECTED_DEPENDENCIES`.
"""

import importlib
import pkgutil
from collections.abc import Sequence
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.errors import install_error_handlers

API_PREFIX = "/api/v1"

PROTECTED_DEPENDENCIES: list[Any] = []
"""`Depends(...)` applied to every protected route; T025 adds `Depends(current_user)`."""


def _route_modules() -> list[Any]:
    """The `app.routes.*` modules, in name order; none until route modules exist."""
    try:
        package = importlib.import_module("app.routes")
    except ModuleNotFoundError:
        return []
    names = sorted(m.name for m in pkgutil.iter_modules(package.__path__))
    return [importlib.import_module(f"app.routes.{name}") for name in names]


def build_routers(
    *,
    public_routers: Sequence[APIRouter] | None = None,
    protected_routers: Sequence[APIRouter] | None = None,
    protected_dependencies: Sequence[Any] | None = None,
) -> tuple[APIRouter, APIRouter]:
    """The public and protected `/api/v1` routers; defaults come from `app.routes`."""
    if public_routers is None or protected_routers is None:
        modules = _route_modules()
        if public_routers is None:
            public_routers = [m.public_router for m in modules if hasattr(m, "public_router")]
        if protected_routers is None:
            protected_routers = [
                m.protected_router for m in modules if hasattr(m, "protected_router")
            ]
    deps = PROTECTED_DEPENDENCIES if protected_dependencies is None else protected_dependencies
    public_router = APIRouter(prefix=API_PREFIX)
    protected_router = APIRouter(prefix=API_PREFIX, dependencies=list(deps))
    for router in public_routers:
        public_router.include_router(router)
    for router in protected_routers:
        protected_router.include_router(router)
    return public_router, protected_router


def create_app() -> FastAPI:
    """The API app: routers, CORS, the Section 11 error handlers; docs only when ALLOW_FAULTS."""
    settings = get_settings()
    docs = settings.ALLOW_FAULTS
    app = FastAPI(
        title="Ledgerline API",
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )
    install_error_handlers(app)
    for router in build_routers():
        if router.routes:  # an empty router would still mount a placeholder route
            app.include_router(router)
    return app


app = create_app()
