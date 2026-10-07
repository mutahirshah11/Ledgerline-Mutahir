"""`BlobStore` protocol (data-model.md Section 7) and the in-memory implementation.

Buckets `inputs` and `reports` (data-model.md Section 5); any other bucket raises ValueError.
A `reports` object is never overwritten (F-09.R5): `put` on an existing path raises
`UniqueViolation`, and `delete` raises `AppendOnlyViolation` (delete-then-put would overwrite).
Signed URLs from `MemoryBlobStore` are fake
`memory://bucket/path?exp=<epoch>` strings; expiry is enforced only by Supabase (Phase 6).
"""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from app.core import clock
from app.core.errors import AppendOnlyViolation, NotFound, UniqueViolation

INPUTS_BUCKET = "inputs"
REPORTS_BUCKET = "reports"
BUCKETS = frozenset({INPUTS_BUCKET, REPORTS_BUCKET})


def _check_bucket(bucket: str) -> None:
    if bucket not in BUCKETS:
        raise ValueError(f"unknown bucket {bucket!r}; expected one of {sorted(BUCKETS)}")


@runtime_checkable
class BlobStore(Protocol):
    def put(self, bucket: str, path: str, data: bytes, content_type: str) -> None: ...
    def get(self, bucket: str, path: str) -> bytes:
        """NotFound."""
        ...

    def exists(self, bucket: str, path: str) -> bool: ...
    def delete(self, bucket: str, path: str) -> None: ...
    def signed_upload_url(self, bucket: str, path: str, expires_s: int = 600) -> str: ...
    def signed_download_url(
        self, bucket: str, path: str, expires_s: int = 300
    ) -> tuple[str, datetime]: ...
    def bucket_ok(self, bucket: str) -> bool:
        """004 E3 (F-11.R7): the bucket exists and is reachable; never raises if missing."""
        ...


class MemoryBlobStore:
    """Dict-backed `BlobStore`. `now` supplies the clock for signed-URL expiry."""

    def __init__(self, now: Callable[[], datetime] = clock.now) -> None:
        self._now = now
        self._objects: dict[tuple[str, str], tuple[bytes, str]] = {}

    def put(self, bucket: str, path: str, data: bytes, content_type: str) -> None:
        _check_bucket(bucket)
        key = (bucket, path)
        if bucket == REPORTS_BUCKET and key in self._objects:
            raise UniqueViolation(f"{bucket}/{path}")
        self._objects[key] = (bytes(data), content_type)

    def get(self, bucket: str, path: str) -> bytes:
        _check_bucket(bucket)
        try:
            return self._objects[(bucket, path)][0]
        except KeyError:
            raise NotFound("blob", f"{bucket}/{path}") from None

    def exists(self, bucket: str, path: str) -> bool:
        _check_bucket(bucket)
        return (bucket, path) in self._objects

    def delete(self, bucket: str, path: str) -> None:
        """Removes an `inputs` object (no-op if absent); `reports` objects are permanent."""
        _check_bucket(bucket)
        if bucket == REPORTS_BUCKET:
            raise AppendOnlyViolation(f"{bucket}/{path}")
        self._objects.pop((bucket, path), None)

    def bucket_ok(self, bucket: str) -> bool:
        return bucket in BUCKETS

    def _url(self, bucket: str, path: str, expires_at: datetime) -> str:
        _check_bucket(bucket)
        return f"memory://{bucket}/{path}?exp={int(expires_at.timestamp())}"

    def signed_upload_url(self, bucket: str, path: str, expires_s: int = 600) -> str:
        return self._url(bucket, path, self._now() + timedelta(seconds=expires_s))

    def signed_download_url(
        self, bucket: str, path: str, expires_s: int = 300
    ) -> tuple[str, datetime]:
        expires_at = self._now() + timedelta(seconds=expires_s)
        return self._url(bucket, path, expires_at), expires_at


if TYPE_CHECKING:  # static conformance check (mypy)
    _conforms: BlobStore = MemoryBlobStore()
