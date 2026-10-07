"""Telegram sender (contracts/alerts.md; F-10.R5, F-11.R4, C21, research R3/R7/R8)."""

from typing import Protocol


class Notifier(Protocol):
    """Sends a plain-text message; returns Telegram's `message_id`. Raises UpstreamFailed
    ("telegram", detail) after its own retries; the detail never contains the bot token."""

    def send_message(self, chat_id: str, text: str) -> int: ...
