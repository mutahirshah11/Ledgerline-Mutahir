"""Shared FastAPI dependency providers (004 data-model §5); tests override them."""

from collections.abc import Iterator

from app.core.config import get_settings
from app.services.telegram import Notifier, TelegramNotifier


def get_notifier() -> Iterator[Notifier]:
    """The Telegram notifier for one request; its HTTP client is closed afterwards."""
    with TelegramNotifier(get_settings()) as notifier:
        yield notifier
