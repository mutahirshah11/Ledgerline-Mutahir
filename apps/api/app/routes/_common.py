"""Helpers shared by the run and review routes (005 data-model.md §8, contracts/openapi.yaml)."""

from typing import Any

from fastapi import Request

from app.core.config import get_settings
from app.domain.enums import AuditChannel

READ_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"description": "UNAUTHENTICATED"},
    403: {"description": "NOT_ALLOWED"},
    404: {"description": "NOT_FOUND"},
}
"""Documented non-2xx status codes shared by EP-11 to EP-14, EP-16 and the writes that add to
them (contracts/openapi.yaml)."""


def channel(request: Request) -> AuditChannel:
    """WEB for a call from the app's browser origin, else API (data-model.md §8). `Origin` only
    labels the audit channel (WEB or API); it is never used to authorise anything."""
    origin = request.headers.get("origin")
    return AuditChannel.WEB if origin in get_settings().cors_origin_list else AuditChannel.API
