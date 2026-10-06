"""The model client seam for page reads (contracts/extract-service.md §1)."""

import base64
import glob
import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx2
import openai

from app.core.config import EXTRACT_TIMEOUT_S, get_settings
from app.core.errors import InvalidReadOutput
from app.domain.enums import Fault, ReadVariant


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


# --- Implementations (T020; research R1, R3) ----------------------------------------------

logger = logging.getLogger(__name__)

SCHEMA_NAME = "register_page_read"
SUPPORTED_MIMES = frozenset({"image/jpeg", "image/png"})
USER_TEXT = "Read this stock register page and answer in the required JSON format."


class OpenAIModelClient:
    """Production client: one Responses API call per read, Structured Outputs, no SDK retries.

    SDK exceptions (timeouts, connection, 429, 5xx, 4xx) surface unchanged so the service's
    retry layer can classify them (contract §4); an answer that is not a JSON object raises
    `InvalidReadOutput`. Never logs the API key or the image.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout_s: float = EXTRACT_TIMEOUT_S,
        *,
        sdk: Any = None,
        timer: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        settings = get_settings()
        if api_key is None and settings.OPENAI_API_KEY is not None:
            api_key = settings.OPENAI_API_KEY.get_secret_value()
        self._api_key = api_key
        self.model = model if model is not None else settings.EXTRACT_MODEL
        self.timeout_s = timeout_s
        self._timer = timer
        # Timeout is set on the client options AND per request on purpose (T019 asserts both).
        self._sdk = None if sdk is None else sdk.with_options(max_retries=0, timeout=timeout_s)

    def __repr__(self) -> str:
        return f"OpenAIModelClient(model={self.model!r}, timeout_s={self.timeout_s!r})"

    def _client(self) -> Any:
        if self._sdk is None:
            if not self._api_key:
                raise RuntimeError("OPENAI_API_KEY is not set; cannot call the model")
            self._sdk = openai.OpenAI(api_key=self._api_key, max_retries=0, timeout=self.timeout_s)
        return self._sdk

    def read_page(
        self,
        *,
        image: bytes,
        mime: str,
        variant: ReadVariant,
        instructions: str,
        schema: dict[str, Any],
    ) -> RawRead:
        if mime not in SUPPORTED_MIMES:
            raise ValueError(f"unsupported image type {mime!r}")
        client = self._client()
        data_url = f"data:{mime};base64,{base64.b64encode(image).decode('ascii')}"
        started = self._timer()
        response = client.responses.create(
            model=self.model,
            instructions=instructions,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": USER_TEXT},
                        {"type": "input_image", "image_url": data_url, "detail": "high"},
                    ],
                }
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": SCHEMA_NAME,
                    "strict": True,
                    "schema": schema,
                }
            },
            store=False,
            timeout=self.timeout_s,
        )
        latency_ms = (self._timer() - started) // 1_000_000
        usage = response.usage
        input_tokens = int(usage.input_tokens) if usage is not None else 0
        output_tokens = int(usage.output_tokens) if usage is not None else 0
        logger.info(
            "model read variant=%s model=%s tokens_in=%d tokens_out=%d latency_ms=%d",
            variant.value,
            self.model,
            input_tokens,
            output_tokens,
            latency_ms,
        )
        try:
            payload = json.loads(response.output_text)
        except (TypeError, ValueError) as exc:
            raise InvalidReadOutput(f"answer is not JSON ({type(exc).__name__})") from None
        if not isinstance(payload, dict):
            raise InvalidReadOutput(f"answer is a JSON {type(payload).__name__}, not an object")
        return RawRead(
            payload=payload,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
        )


class CachedReadAmbiguous(Exception):
    """More than one committed read matches; pass `model` and `prompt_version` to narrow it.

    Deliberately not a `LookupError`, so callers catching a miss (`KeyError`) never swallow it.
    """


class ReplayModelClient:
    """Offline client: answers from committed `Extraction` files (research R10).

    Files are `{sha256}.{variant}.{model}.{prompt_version}.json`; `model` / `prompt_version`
    narrow the match when given (matched literally, glob characters escaped).

    Raises:
        KeyError: no committed read for this image and variant (a cache miss).
        CachedReadAmbiguous: more than one file matches.
        ValueError: the matched file's `page_sha256` / `read_variant` disagree with the request,
            or it has no `result`; the message names the file.
    """

    def __init__(
        self, cache_dir: Path, *, model: str | None = None, prompt_version: str | None = None
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.model = model
        self.prompt_version = prompt_version
        self.calls = 0

    def read_page(
        self,
        *,
        image: bytes,
        mime: str,
        variant: ReadVariant,
        instructions: str,
        schema: dict[str, Any],
    ) -> RawRead:
        self.calls += 1
        sha = hashlib.sha256(image).hexdigest()
        model = glob.escape(self.model) if self.model else "*"
        prompt_version = glob.escape(self.prompt_version) if self.prompt_version else "*"
        # TODO(T029): derive the pattern from extract.cache_file_name
        pattern = f"{sha}.{variant.value}.{model}.{prompt_version}.json"
        matches = sorted(self.cache_dir.glob(pattern))
        if not matches:
            raise KeyError(f"no cached read for {sha[:12]} variant {variant.value}")
        if len(matches) > 1:
            names = ", ".join(path.name for path in matches)
            raise CachedReadAmbiguous(
                f"ambiguous cached reads ({names}); pass model and prompt_version"
            )
        path = matches[0]
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("page_sha256") != sha:
            raise ValueError(f"cached read {path.name}: page_sha256 does not match the image")
        if record.get("read_variant") != variant.value:
            raise ValueError(f"cached read {path.name}: read_variant is not {variant.value}")
        if not isinstance(record.get("result"), dict):
            raise ValueError(f"cached read {path.name}: missing result object")
        return RawRead(
            payload=record["result"],
            input_tokens=int(record["input_tokens"]),
            output_tokens=int(record["output_tokens"]),
            latency_ms=int(record["latency_ms"]),
        )


EXTRACT_FAULTS = frozenset({Fault.EXTRACT_TIMEOUT, Fault.EXTRACT_BAD_JSON})
BAD_JSON_PAYLOAD: dict[str, Any] = {"rows": "not a list"}
_FAULT_REQUEST_URL = "https://api.openai.com/v1/responses"


class FaultModelClient:
    """Phase 3 `?fault=` wrapper: the same failure on every call (contract §1).

    `EXTRACT_TIMEOUT` raises `openai.APITimeoutError`; `EXTRACT_BAD_JSON` returns a payload
    that fails read validation (FM-05). The inner client is never called.
    """

    def __init__(self, inner: ModelClient, fault: Fault) -> None:
        if fault not in EXTRACT_FAULTS:
            raise ValueError(f"{fault} is not an extraction fault")
        self.inner = inner
        self.fault = fault

    def read_page(
        self,
        *,
        image: bytes,
        mime: str,
        variant: ReadVariant,
        instructions: str,
        schema: dict[str, Any],
    ) -> RawRead:
        if self.fault is Fault.EXTRACT_TIMEOUT:
            raise openai.APITimeoutError(request=httpx2.Request("POST", _FAULT_REQUEST_URL))
        return RawRead(
            payload=dict(BAD_JSON_PAYLOAD), input_tokens=0, output_tokens=0, latency_ms=0
        )
