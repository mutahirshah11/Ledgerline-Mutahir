"""EP-02 `GET /me` (F-12, contracts/openapi.yaml `Me`); EP-01 `GET /health` arrives in T052."""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import current_user
from app.domain.enums import Role
from app.domain.models import AllowedUser

protected_router = APIRouter()


class Me(BaseModel):
    email: str
    display_name: str
    role: Role


@protected_router.get("/me", response_model=Me)
def me(user: Annotated[AllowedUser, Depends(current_user)]) -> Me:
    """EP-02: the signed-in caller's allowlist entry."""
    return Me(email=user.email, display_name=user.display_name, role=user.role)
