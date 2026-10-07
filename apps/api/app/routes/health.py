"""EP-01 `GET /health` (F-11.R7, FM-14, research R10) and EP-02 `GET /me` (F-12)."""

from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from app.core import clock
from app.core.auth import current_user
from app.core.config import get_settings
from app.core.db import Repo, get_repo
from app.core.storage import BlobStore, get_blobstore
from app.domain.enums import NotificationKind, NotificationStatus, Role
from app.domain.models import AllowedUser, Notification

public_router = APIRouter()
protected_router = APIRouter()

STUCK_SENDING_AFTER = timedelta(minutes=5)
"""contracts/alerts.md: an ALERT notification stuck in SENDING longer than this is failing."""


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    db: Literal["ok", "failing"]
    storage: Literal["ok", "failing"]
    openai_key_set: bool
    telegram_set: bool
    telegram: Literal["ok", "failing", "unset"]
    scenario_now: datetime


def _telegram_state(
    latest: Notification | None, telegram_set: bool
) -> Literal["ok", "failing", "unset"]:
    if latest is not None:
        stuck = (
            latest.status == NotificationStatus.SENDING
            and clock.now() - latest.updated_at > STUCK_SENDING_AFTER
        )
        if latest.status == NotificationStatus.FAILED or stuck:
            return "failing"
    return "ok" if telegram_set else "unset"


@public_router.get("/health", response_model=Health)
def health(
    response: Response,
    repo: Annotated[Repo, Depends(get_repo)],
    blobstore: Annotated[BlobStore, Depends(get_blobstore)],
) -> Health:
    """EP-01: no authentication. 200 when db and storage are ok, else 503 (same body)."""
    settings = get_settings()
    db_ok = repo.ping()
    storage_ok = blobstore.bucket_ok("inputs") and blobstore.bucket_ok("reports")
    openai_key_set = bool(settings.OPENAI_API_KEY and settings.OPENAI_API_KEY.get_secret_value())
    telegram_set = bool(
        settings.TELEGRAM_BOT_TOKEN
        and settings.TELEGRAM_BOT_TOKEN.get_secret_value()
        and settings.TELEGRAM_OPS_CHAT_ID
        and settings.TELEGRAM_CONTROLLER_CHAT_ID
    )
    latest_alert = repo.get_latest_notification(NotificationKind.ALERT)
    body = Health(
        status="ok" if db_ok and storage_ok else "degraded",
        db="ok" if db_ok else "failing",
        storage="ok" if storage_ok else "failing",
        openai_key_set=openai_key_set,
        telegram_set=telegram_set,
        telegram=_telegram_state(latest_alert, telegram_set),
        scenario_now=clock.now(),
    )
    response.status_code = 200 if body.status == "ok" else 503
    return body


class Me(BaseModel):
    email: str
    display_name: str
    role: Role


@protected_router.get("/me", response_model=Me)
def me(user: Annotated[AllowedUser, Depends(current_user)]) -> Me:
    """EP-02: the signed-in caller's allowlist entry."""
    return Me(email=user.email, display_name=user.display_name, role=user.role)
