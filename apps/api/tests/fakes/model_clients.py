"""ScriptedModelClient: a ModelClient fed by per-variant queues (extract-service.md §1)."""

import hashlib
import threading
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.domain.enums import ReadVariant
from app.pipeline.model_client import RawRead

Answer = RawRead | dict[str, Any] | BaseException
"""A scripted answer: a RawRead, a bare payload (zero usage) or an exception to raise."""


@dataclass(frozen=True)
class RecordedRequest:
    image_sha256: str
    mime: str
    variant: ReadVariant
    instructions: str
    schema: dict[str, Any]


class ScriptedModelClient:
    """Answers each variant from its own queue, in order; records every request.

    With `barrier`, EVERY call waits on it before answering, so a `Barrier(2)` proves two
    calls ran at the same time (a sequential caller gets `BrokenBarrierError`). Size the
    barrier to the total number of calls the test makes (or a divisor of it), and give it a
    `timeout`: a barrier without one is rejected (ValueError) so a mis-sized barrier fails
    the test instead of hanging it.
    """

    def __init__(
        self,
        script: Mapping[ReadVariant, Sequence[Answer]] | None = None,
        *,
        barrier: threading.Barrier | None = None,
    ) -> None:
        if barrier is not None and getattr(barrier, "_timeout", None) is None:
            raise ValueError("barrier needs a timeout, e.g. threading.Barrier(2, timeout=5)")
        self._queues: dict[ReadVariant, deque[Answer]] = {v: deque() for v in ReadVariant}
        for variant, answers in (script or {}).items():
            self._queues[variant].extend(answers)
        self._barrier = barrier
        self._lock = threading.Lock()
        self.requests: list[RecordedRequest] = []

    @property
    def calls(self) -> int:
        return len(self.requests)

    def push(self, variant: ReadVariant, *answers: Answer) -> None:
        with self._lock:
            self._queues[variant].extend(answers)

    def read_page(
        self,
        *,
        image: bytes,
        mime: str,
        variant: ReadVariant,
        instructions: str,
        schema: dict[str, Any],
    ) -> RawRead:
        request = RecordedRequest(
            hashlib.sha256(image).hexdigest(), mime, variant, instructions, schema
        )
        with self._lock:
            self.requests.append(request)
            queue = self._queues[variant]
            answer = queue.popleft() if queue else None
        if self._barrier is not None:
            self._barrier.wait()
        if answer is None:
            raise AssertionError(f"no scripted answer for variant {variant.value}")
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, RawRead):
            return answer
        return RawRead(payload=answer, input_tokens=0, output_tokens=0, latency_ms=0)
