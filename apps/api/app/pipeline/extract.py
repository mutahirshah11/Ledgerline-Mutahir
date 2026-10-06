"""F-03 extraction service (contracts/extract-service.md). Pure functions make no I/O except
reading the shipped prompt and schema files."""

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.core.errors import InvalidReadOutput
from app.domain.enums import ReadVariant
from app.domain.models import PageRead

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def load_prompt(variant: ReadVariant, version: str) -> str:
    """Return the instructions for one read variant at one prompt version (F-03.R2).

    Files are named `read_{variant}_{version}.md`; a missing file raises FileNotFoundError.
    """
    path = PROMPTS_DIR / f"read_{variant.value.lower()}_{version}.md"
    return path.read_text(encoding="utf-8")


def load_read_schema(version: str) -> dict[str, Any]:
    """Return the strict read schema shipped for a prompt version (F-03.R4).

    Files are named `read_schema_{version}.json`; a missing file raises FileNotFoundError.
    """
    path = PROMPTS_DIR / f"read_schema_{version}.json"
    schema: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return schema


def parse_read(payload: object) -> PageRead:
    """Validate one model answer as a `PageRead` (data-model §1, FM-05).

    Accepts the parsed JSON object (or its JSON text). Any failure, including text that is not
    a JSON object, raises `InvalidReadOutput`; rows come back sorted by `row_index`.
    """
    if isinstance(payload, str | bytes):
        try:
            payload = json.loads(payload)
        except ValueError as exc:
            raise InvalidReadOutput(f"not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise InvalidReadOutput(f"expected a JSON object, got {type(payload).__name__}")
    try:
        return PageRead.model_validate(payload)
    except ValidationError as exc:
        raise InvalidReadOutput(str(exc)) from exc
