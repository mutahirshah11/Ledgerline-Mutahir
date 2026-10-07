"""The app's effective route table (004 T022). FastAPI mounts included routers lazily, so
`app.routes` lists `_IncludedRouter` wrappers; their `effective_route_contexts()` carry the full
path, methods and dependency tree (router-level dependencies included)."""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute


@dataclass(frozen=True)
class RouteInfo:
    path: str
    methods: frozenset[str]
    dependant: Dependant

    def calls(self) -> Iterator[Any]:
        """Every dependency callable in the route's tree, depth first."""
        stack = list(self.dependant.dependencies)
        while stack:
            dep = stack.pop()
            yield dep.call
            stack.extend(dep.dependencies)


def api_routes(app: FastAPI) -> list[RouteInfo]:
    """Direct `APIRoute`s plus every effective route of the included routers."""
    found: list[RouteInfo] = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            found.append(RouteInfo(route.path, frozenset(route.methods or ()), route.dependant))
        elif hasattr(route, "effective_route_contexts"):
            for ctx in route.effective_route_contexts():
                if isinstance(ctx.original_route, APIRoute) and ctx.dependant is not None:
                    found.append(RouteInfo(ctx.path, frozenset(ctx.methods), ctx.dependant))
    return found
