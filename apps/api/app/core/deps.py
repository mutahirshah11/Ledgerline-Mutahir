"""Shared FastAPI dependency providers (004 data-model.md §5); tests override them."""

import time
from collections.abc import Callable, Iterator

from app.core.config import get_settings
from app.pipeline.model_client import ModelClient, OpenAIModelClient
from app.services.telegram import Notifier, TelegramNotifier


def get_notifier() -> Iterator[Notifier]:
    """The Telegram notifier for one request; its HTTP client is closed afterwards."""
    with TelegramNotifier(get_settings()) as notifier:
        yield notifier


def get_model_client() -> ModelClient:
    """EP-09's page reader: OpenAI with the `OPENAI_TIMEOUT_S` per call (NFR-03, T049). The SDK
    client is built on the first call, so a cached page never needs the key; tests override
    this with a replay client."""
    return OpenAIModelClient(timeout_s=get_settings().OPENAI_TIMEOUT_S)


def get_extract_sleep() -> Callable[[float], None]:
    """How EP-09 waits between read attempts (`EXTRACT_BACKOFF_S`); tests skip the waits."""
    return time.sleep
