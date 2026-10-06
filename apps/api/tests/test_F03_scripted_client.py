"""ScriptedModelClient, the test double for ModelClient (contracts/extract-service.md §1)."""

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.domain.enums import ReadVariant
from app.pipeline.model_client import ModelClient, RawRead
from tests.fakes.model_clients import ScriptedModelClient

IMAGE = b"page-image-bytes"
SCHEMA: dict[str, object] = {"type": "object"}


def _read(client: ModelClient, variant: ReadVariant, instructions: str = "rules") -> RawRead:
    return client.read_page(
        image=IMAGE, mime="image/jpeg", variant=variant, instructions=instructions, schema=SCHEMA
    )


def test_F03_scripted_client_answers_each_variant_in_queue_order() -> None:
    first = RawRead(payload={"n": 1}, input_tokens=10, output_tokens=5, latency_ms=7)
    client = ScriptedModelClient({ReadVariant.A: [first, {"n": 2}], ReadVariant.B: [{"n": "b"}]})

    assert _read(client, ReadVariant.B).payload == {"n": "b"}
    assert _read(client, ReadVariant.A) == first
    second = _read(client, ReadVariant.A)
    assert second.payload == {"n": 2}
    assert (second.input_tokens, second.output_tokens, second.latency_ms) == (0, 0, 0)


def test_F03_scripted_client_raises_scripted_exceptions() -> None:
    boom = TimeoutError("slow")
    client = ScriptedModelClient({ReadVariant.A: [boom, {"ok": True}]})

    with pytest.raises(TimeoutError) as caught:
        _read(client, ReadVariant.A)
    assert caught.value is boom
    assert _read(client, ReadVariant.A).payload == {"ok": True}


def test_F03_scripted_client_fails_loudly_when_queue_is_empty() -> None:
    client = ScriptedModelClient({ReadVariant.A: []})

    with pytest.raises(AssertionError, match="no scripted answer for variant B"):
        _read(client, ReadVariant.B)


def test_F03_scripted_client_records_every_request() -> None:
    client = ScriptedModelClient({ReadVariant.A: [{}], ReadVariant.B: [RuntimeError("x")]})
    client.push(ReadVariant.A, {"later": 1})

    _read(client, ReadVariant.A, "rules A")
    with pytest.raises(RuntimeError):
        _read(client, ReadVariant.B, "rules B")
    _read(client, ReadVariant.A, "rules A")

    sha = hashlib.sha256(IMAGE).hexdigest()
    assert [(r.image_sha256, r.variant, r.instructions) for r in client.requests] == [
        (sha, ReadVariant.A, "rules A"),
        (sha, ReadVariant.B, "rules B"),
        (sha, ReadVariant.A, "rules A"),
    ]
    assert client.calls == 3
    assert client.requests[0].mime == "image/jpeg"
    assert client.requests[0].schema == SCHEMA


def test_F03_scripted_client_barrier_proves_two_calls_overlap() -> None:
    client = ScriptedModelClient(
        {ReadVariant.A: [{"v": "A"}], ReadVariant.B: [{"v": "B"}]},
        barrier=threading.Barrier(2, timeout=5),
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_read, client, v) for v in (ReadVariant.A, ReadVariant.B)]
        payloads = [f.result().payload for f in futures]

    assert payloads == [{"v": "A"}, {"v": "B"}]


def test_F03_scripted_client_barrier_breaks_when_calls_are_sequential() -> None:
    client = ScriptedModelClient(
        {ReadVariant.A: [{"v": "A"}]}, barrier=threading.Barrier(2, timeout=0.05)
    )

    with pytest.raises(threading.BrokenBarrierError):
        _read(client, ReadVariant.A)
