"""F-03.R1 / R2 model clients (contracts/extract-service.md §1, research R1, R3). Offline only."""

import ast
import base64
import hashlib
import json
import logging
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import pytest

from app.core.config import EXTRACT_TIMEOUT_S, Settings
from app.core.errors import InvalidReadOutput
from app.domain.enums import Fault, ReadVariant
from app.pipeline.model_client import (
    USER_TEXT,
    CachedReadAmbiguous,
    FaultModelClient,
    OpenAIModelClient,
    RawRead,
    ReplayModelClient,
)

API_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "app" / "pipeline" / "prompts"
SCHEMA_FILE = SCHEMA_PATH / "read_schema_v1.json"
JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-body" * 8
PNG = b"\x89PNG\r\n\x1a\n" + b"png-body" * 8
API_KEY = "-".join(["unit", "test", "credential", "value"])  # built, not a literal secret
PAYLOAD: dict[str, Any] = {"rows": [], "month_written": None}


class FakeResponses:
    def __init__(self, owner: "FakeSDK") -> None:
        self.owner = owner

    def create(self, **kwargs: Any) -> Any:
        self.owner.create_calls.append(kwargs)
        return SimpleNamespace(output_text=self.owner.output_text, usage=self.owner.usage)


class FakeSDK:
    """Stands in for `openai.OpenAI`: records `with_options` and `responses.create` kwargs."""

    def __init__(self, output_text: str = json.dumps(PAYLOAD)) -> None:
        self.output_text = output_text
        self.usage: Any = SimpleNamespace(input_tokens=1234, output_tokens=567)
        self.options: dict[str, Any] = {}
        self.create_calls: list[dict[str, Any]] = []
        self.responses = FakeResponses(self)

    def with_options(self, **kwargs: Any) -> "FakeSDK":
        self.options.update(kwargs)
        return self


def _schema() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
    return loaded


def _without_comments(node: Any) -> Any:
    """The schema minus every `$comment` annotation, at any depth."""
    if isinstance(node, dict):
        return {k: _without_comments(v) for k, v in node.items() if k != "$comment"}
    if isinstance(node, list):
        return [_without_comments(v) for v in node]
    return node


def _ticks(*values: int) -> Any:
    it = iter(values)
    return lambda: next(it)


def _client(sdk: FakeSDK, **kwargs: Any) -> OpenAIModelClient:
    return OpenAIModelClient(api_key=API_KEY, model=Settings().EXTRACT_MODEL, sdk=sdk, **kwargs)


# --- OpenAIModelClient -----------------------------------------------------------------------


def test_F03_R1_openai_request_shape() -> None:
    sdk = FakeSDK()
    schema = _schema()
    client = _client(sdk)

    client.read_page(
        image=JPEG, mime="image/jpeg", variant=ReadVariant.A, instructions="RULES", schema=schema
    )

    assert sdk.options == {"max_retries": 0, "timeout": EXTRACT_TIMEOUT_S}
    assert EXTRACT_TIMEOUT_S == 60
    (call,) = sdk.create_calls
    assert call["model"] == Settings().EXTRACT_MODEL
    assert call["instructions"] == "RULES"
    assert call["store"] is False
    assert call["timeout"] == 60
    assert call["text"] == {
        "format": {
            "type": "json_schema",
            "name": "register_page_read",
            "strict": True,
            "schema": _without_comments(schema),
        }
    }
    assert "$comment" not in json.dumps(call["text"])
    assert "$defs" in call["text"]["format"]["schema"]  # keywords other than $comment are kept
    assert schema == _schema()  # the caller's schema is not mutated
    (message,) = call["input"]
    assert message["role"] == "user"
    texts = [part["text"] for part in message["content"] if part["type"] == "input_text"]
    assert texts == [USER_TEXT]
    images = [part for part in message["content"] if part["type"] == "input_image"]
    (image,) = images
    assert image["detail"] == "high"
    assert image["image_url"] == "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()
    for sampling in ("temperature", "top_p"):
        assert sampling not in call


def test_F03_R3_png_data_url_uses_given_mime() -> None:
    sdk = FakeSDK()
    _client(sdk).read_page(
        image=PNG, mime="image/png", variant=ReadVariant.B, instructions="x", schema=_schema()
    )
    content = sdk.create_calls[0]["input"][0]["content"]
    image = next(part for part in content if part["type"] == "input_image")
    assert image["image_url"].startswith("data:image/png;base64,")


def test_F03_R3_unsupported_mime_rejected_before_call() -> None:
    sdk = FakeSDK()
    with pytest.raises(ValueError):
        _client(sdk).read_page(
            image=b"GIF89a", mime="image/gif", variant=ReadVariant.A, instructions="x", schema={}
        )
    assert sdk.create_calls == []


def test_F03_R1_returns_payload_tokens_and_monotonic_latency() -> None:
    client = _client(FakeSDK(), timer=_ticks(1_000_000_000, 1_250_400_000))
    read = client.read_page(
        image=JPEG, mime="image/jpeg", variant=ReadVariant.A, instructions="x", schema=_schema()
    )
    assert read == RawRead(payload=PAYLOAD, input_tokens=1234, output_tokens=567, latency_ms=250)


def test_F03_R1_missing_usage_yields_zero_tokens() -> None:
    sdk = FakeSDK()
    sdk.usage = None
    read = _client(sdk, timer=_ticks(0, 0)).read_page(
        image=JPEG, mime="image/jpeg", variant=ReadVariant.A, instructions="x", schema={}
    )
    assert (read.input_tokens, read.output_tokens) == (0, 0)


@pytest.mark.parametrize("text", ["not json at all", "[1, 2]", ""])
def test_F03_FM05_unparseable_output_raises_invalid_read_output(text: str) -> None:
    client = _client(FakeSDK(output_text=text))
    with pytest.raises(InvalidReadOutput):
        client.read_page(
            image=JPEG, mime="image/jpeg", variant=ReadVariant.A, instructions="x", schema={}
        )


def test_F03_R5_sdk_exceptions_surface_unchanged() -> None:
    error = openai.APITimeoutError(request=_request())

    class Raising(FakeSDK):
        def __init__(self) -> None:
            super().__init__()
            self.responses = SimpleNamespace(create=self._raise)  # type: ignore[assignment]

        def _raise(self, **kwargs: Any) -> Any:
            raise error

    with pytest.raises(openai.APITimeoutError) as caught:
        _client(Raising()).read_page(
            image=JPEG, mime="image/jpeg", variant=ReadVariant.A, instructions="x", schema={}
        )
    assert caught.value is error


def test_F03_R1_defaults_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXTRACT_MODEL", "model-from-env")
    monkeypatch.setenv("OPENAI_API_KEY", API_KEY)
    from app.core.config import get_settings

    get_settings.cache_clear()
    sdk = FakeSDK()
    OpenAIModelClient(sdk=sdk).read_page(
        image=JPEG, mime="image/jpeg", variant=ReadVariant.A, instructions="x", schema={}
    )
    assert sdk.create_calls[0]["model"] == "model-from-env"


def test_F03_R1_builds_sdk_lazily_with_no_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[dict[str, Any]] = []

    def factory(**kwargs: Any) -> FakeSDK:
        built.append(kwargs)
        return FakeSDK()

    monkeypatch.setattr(openai, "OpenAI", factory)
    client = OpenAIModelClient(api_key=API_KEY, model="m")
    assert built == []
    client.read_page(
        image=JPEG, mime="image/jpeg", variant=ReadVariant.A, instructions="x", schema={}
    )
    client.read_page(
        image=JPEG, mime="image/jpeg", variant=ReadVariant.B, instructions="x", schema={}
    )
    assert built == [{"api_key": API_KEY, "max_retries": 0, "timeout": EXTRACT_TIMEOUT_S}]


def test_F03_R1_concurrent_reads_build_one_sdk_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both read threads ask for the SDK at once; only one client is ever built."""
    built: list[FakeSDK] = []
    start = threading.Barrier(2, timeout=5)

    def factory(**_: Any) -> FakeSDK:
        time.sleep(0.05)  # widen the window in which a second thread could also build one
        sdk = FakeSDK()
        built.append(sdk)
        return sdk

    monkeypatch.setattr(openai, "OpenAI", factory)
    client = OpenAIModelClient(api_key=API_KEY, model="m")
    errors: list[BaseException] = []

    def read(variant: ReadVariant) -> None:
        try:
            start.wait()
            client.read_page(
                image=JPEG, mime="image/jpeg", variant=variant, instructions="x", schema={}
            )
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=read, args=(v,)) for v in ReadVariant]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert errors == []
    assert len(built) == 1
    assert len(built[0].create_calls) == 2


def test_F03_R1_missing_api_key_raises_clear_error() -> None:
    client = OpenAIModelClient(model="m")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        client.read_page(
            image=JPEG, mime="image/jpeg", variant=ReadVariant.A, instructions="x", schema={}
        )


def test_F03_logs_contain_no_key_or_image_bytes(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    client = _client(FakeSDK())
    client.read_page(
        image=JPEG, mime="image/jpeg", variant=ReadVariant.A, instructions="x", schema=_schema()
    )
    with pytest.raises(InvalidReadOutput):
        _client(FakeSDK(output_text="nope")).read_page(
            image=JPEG, mime="image/jpeg", variant=ReadVariant.A, instructions="x", schema={}
        )
    text = caplog.text + repr(client)
    assert API_KEY not in text
    assert base64.b64encode(JPEG).decode() not in text
    assert "jpeg-body" not in text


# --- ReplayModelClient -----------------------------------------------------------------------


def _write_record(
    directory: Path,
    image: bytes,
    variant: ReadVariant,
    model: str = "m",
    **overrides: Any,
) -> Path:
    sha = hashlib.sha256(image).hexdigest()
    record: dict[str, Any] = {
        "id": "00000000-0000-0000-0000-000000000001",
        "page_sha256": sha,
        "read_variant": variant.value,
        "model": model,
        "prompt_version": "v1",
        "result": {"rows": [], "variant": variant.value},
        "input_tokens": 10,
        "output_tokens": 20,
        "latency_ms": 30,
        "created_at": "2026-10-01T09:00:00+05:00",
    }
    record.update(overrides)
    record = {key: value for key, value in record.items() if value is not _DROP}
    path = directory / f"{sha}.{variant.value}.{model}.v1.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


_DROP = object()


def test_F03_replay_hit_returns_committed_read(tmp_path: Path) -> None:
    _write_record(tmp_path, JPEG, ReadVariant.A)
    _write_record(tmp_path, JPEG, ReadVariant.B)
    client = ReplayModelClient(tmp_path)

    read = client.read_page(
        image=JPEG, mime="image/jpeg", variant=ReadVariant.B, instructions="x", schema={}
    )

    assert read == RawRead(
        payload={"rows": [], "variant": "B"}, input_tokens=10, output_tokens=20, latency_ms=30
    )
    assert client.calls == 1


def test_F03_replay_miss_raises_key_error(tmp_path: Path) -> None:
    _write_record(tmp_path, JPEG, ReadVariant.A)
    client = ReplayModelClient(tmp_path)
    with pytest.raises(KeyError, match="no cached read"):
        client.read_page(
            image=PNG, mime="image/png", variant=ReadVariant.A, instructions="x", schema={}
        )
    with pytest.raises(KeyError):
        client.read_page(
            image=JPEG, mime="image/jpeg", variant=ReadVariant.B, instructions="x", schema={}
        )
    assert client.calls == 2


def test_F03_replay_filters_by_model_when_ambiguous(tmp_path: Path) -> None:
    _write_record(tmp_path, JPEG, ReadVariant.A, model="m1")
    _write_record(tmp_path, JPEG, ReadVariant.A, model="m2")
    with pytest.raises(CachedReadAmbiguous, match="ambiguous") as caught:
        ReplayModelClient(tmp_path).read_page(
            image=JPEG, mime="image/jpeg", variant=ReadVariant.A, instructions="x", schema={}
        )
    assert not isinstance(caught.value, LookupError)  # distinguishable from a miss
    client = ReplayModelClient(tmp_path, model="m2", prompt_version="v1")
    read = client.read_page(
        image=JPEG, mime="image/jpeg", variant=ReadVariant.A, instructions="x", schema={}
    )
    assert read.input_tokens == 10


def test_F03_replay_escapes_glob_metacharacters(tmp_path: Path) -> None:
    _write_record(tmp_path, JPEG, ReadVariant.A, model="m1")
    _write_record(tmp_path, JPEG, ReadVariant.A, model="m[1]")
    client = ReplayModelClient(tmp_path, model="m[1]", prompt_version="v1")
    client.read_page(
        image=JPEG, mime="image/jpeg", variant=ReadVariant.A, instructions="x", schema={}
    )
    with pytest.raises(KeyError):
        ReplayModelClient(tmp_path, model="m?", prompt_version="v1").read_page(
            image=JPEG, mime="image/jpeg", variant=ReadVariant.A, instructions="x", schema={}
        )


@pytest.mark.parametrize(
    "overrides",
    [{"page_sha256": "0" * 64}, {"read_variant": "B"}, {"result": _DROP}],
    ids=["sha-mismatch", "variant-mismatch", "missing-result"],
)
def test_F03_replay_rejects_inconsistent_record(tmp_path: Path, overrides: dict[str, Any]) -> None:
    path = _write_record(tmp_path, JPEG, ReadVariant.A, **overrides)
    with pytest.raises(ValueError, match=re.escape(path.name)):
        ReplayModelClient(tmp_path).read_page(
            image=JPEG, mime="image/jpeg", variant=ReadVariant.A, instructions="x", schema={}
        )


# --- FaultModelClient ------------------------------------------------------------------------


class CountingClient:
    def __init__(self) -> None:
        self.calls = 0

    def read_page(self, **kwargs: Any) -> RawRead:
        self.calls += 1
        return RawRead(payload=PAYLOAD, input_tokens=1, output_tokens=1, latency_ms=1)


def test_F03_R5_fault_timeout_raises_every_call() -> None:
    inner = CountingClient()
    client = FaultModelClient(inner, Fault.EXTRACT_TIMEOUT)
    for _ in range(3):
        with pytest.raises(openai.APITimeoutError):
            client.read_page(
                image=JPEG, mime="image/jpeg", variant=ReadVariant.A, instructions="x", schema={}
            )
    assert inner.calls == 0


def test_F03_FM05_fault_bad_json_returns_invalid_payload_every_call() -> None:
    client = FaultModelClient(CountingClient(), Fault.EXTRACT_BAD_JSON)
    for variant in (ReadVariant.A, ReadVariant.B, ReadVariant.A):
        read = client.read_page(
            image=JPEG, mime="image/jpeg", variant=variant, instructions="x", schema={}
        )
        assert read.payload == {"rows": "not a list"}


def test_F03_fault_client_rejects_non_extract_fault() -> None:
    other = next(f for f in Fault if not f.value.startswith("EXTRACT_"))
    with pytest.raises(ValueError):
        FaultModelClient(CountingClient(), other)


def _request() -> Any:
    return httpx2.Request("POST", "https://api.openai.com/v1/responses")


EXTRACT_MODULE = "app.pipeline.extract"


def _imports_extract(source: str, package: str = "app.pipeline") -> bool:
    """True when `source` imports the extract module anywhere (module level or nested)."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            if any(alias.name == EXTRACT_MODULE for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:  # relative: resolve against the module's package
                parent = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
                base = f"{parent}.{base}" if base else parent
            if base == EXTRACT_MODULE or base.startswith(EXTRACT_MODULE + "."):
                return True
            if any(f"{base}.{alias.name}" == EXTRACT_MODULE for alias in node.names):
                return True
    return False


@pytest.mark.parametrize(
    "snippet",
    [
        "import app.pipeline.extract",
        "from app.pipeline.extract import parse_read",
        "from app.pipeline import extract",
        "from . import extract",
        "from .extract import merge_reads",
        "def f():\n    from app.pipeline.extract import parse_read\n",
    ],
)
def test_F03_import_check_catches_extract_imports(snippet: str) -> None:
    assert _imports_extract(snippet)


def test_F03_model_client_does_not_import_extract() -> None:
    """NFR-11, no import cycle: the client module stands alone (extract imports it, not vice
    versa). A static check of every import statement, module level or nested."""
    source = (API_ROOT / "app" / "pipeline" / "model_client.py").read_text(encoding="utf-8")
    assert not _imports_extract(source)
