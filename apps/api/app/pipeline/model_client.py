"""The model client seam for page reads (contracts/extract-service.md §1)."""

from dataclasses import dataclass
from typing import Any, Protocol

from app.domain.enums import ReadVariant


@dataclass(frozen=True)
class RawRead:
    """One model answer: parsed JSON (not yet validated) plus usage."""

    payload: dict[str, Any]
    input_tokens: int
    output_tokens: int
    latency_ms: int


class ModelClient(Protocol):
    """Reads one register page image with one prompt variant."""

    def read_page(
        self,
        *,
        image: bytes,
        mime: str,
        variant: ReadVariant,
        instructions: str,
        schema: dict[str, Any],
    ) -> RawRead: ...
