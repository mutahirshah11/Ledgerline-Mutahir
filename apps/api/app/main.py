"""FastAPI entrypoint (004 plan.md, FR-026, NFR-04, blueprint Section 10).

Route modules (`app/routes/*.py`) expose plain `APIRouter`s without a prefix; they are imported
explicitly below and listed in `PUBLIC_ROUTERS` / `PROTECTED_ROUTERS`. `create_app()` mounts them
under `/api/v1`, the protected ones behind `PROTECTED_DEPENDENCIES`.
"""

from collections.abc import Sequence
from typing import Any

from fastapi import APIRouter, Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.auth import current_user
from app.core.config import get_settings
from app.core.errors import install_error_handlers
from app.core.logging import RequestLogMiddleware, configure_logging
from app.routes import health, runs

API_PREFIX = "/api/v1"

PUBLIC_ROUTERS: tuple[APIRouter, ...] = ()
"""No auth; EP-01 adds `health.public_router` here (T052)."""

PROTECTED_ROUTERS: tuple[APIRouter, ...] = (health.protected_router, runs.router)
"""Behind `PROTECTED_DEPENDENCIES`: EP-02 `/me`; EP-03 to EP-07 `runs`."""

PROTECTED_DEPENDENCIES: tuple[Any, ...] = (Depends(current_user),)
"""Applied to every protected route (F-12 AC1, AC2); tests swap the dependency through
`app.dependency_overrides`, never by editing this tuple."""


def build_routers(
    *,
    public_routers: Sequence[APIRouter] | None = None,
    protected_routers: Sequence[APIRouter] | None = None,
    protected_dependencies: Sequence[Any] | None = None,
) -> tuple[APIRouter, APIRouter]:
    """The public and protected `/api/v1` routers; defaults are the module-level tuples."""
    publics = PUBLIC_ROUTERS if public_routers is None else public_routers
    protecteds = PROTECTED_ROUTERS if protected_routers is None else protected_routers
    deps = PROTECTED_DEPENDENCIES if protected_dependencies is None else protected_dependencies
    public_router = APIRouter(prefix=API_PREFIX)
    protected_router = APIRouter(prefix=API_PREFIX, dependencies=list(deps))
    for router in publics:
        public_router.include_router(router)
    for router in protecteds:
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
    # 500s come from Starlette's outermost ServerErrorMiddleware, so they carry no CORS headers.
    configure_logging(settings)
    app.add_middleware(RequestLogMiddleware)  # outermost: one JSON line per request (F-11.R3)
    install_error_handlers(app)
    for router in build_routers():
        if router.routes:  # an empty router would still mount a placeholder route
            app.include_router(router)
    return app


app = create_app()
