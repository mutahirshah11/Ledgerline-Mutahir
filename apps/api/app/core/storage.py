"""`BlobStore` protocol (data-model.md Section 7) and the in-memory implementation.

Buckets `inputs` and `reports` (data-model.md Section 5); any other bucket raises ValueError.
A `reports` object is never overwritten (F-09.R5): `put` on an existing path raises
`UniqueViolation`, and `delete` raises `AppendOnlyViolation` (delete-then-put would overwrite).
Signed URLs from `MemoryBlobStore` are fake
`memory://bucket/path?exp=<epoch>` strings; expiry is enforced only by Supabase (Phase 6).
`SupabaseBlobStore` (T065) is the same contract over Supabase Storage with the service-role key.
"""

import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, runtime_checkable

from app.core import clock
from app.core.config import Settings, get_settings
from app.core.errors import AppendOnlyViolation, NotFound, UniqueViolation

logger = logging.getLogger(__name__)
T = TypeVar("T")

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


def _status(error: Exception) -> str:
    """The HTTP status of a storage3 `StorageApiError` ("" for anything else)."""
    return str(getattr(error, "status", ""))


class SupabaseBlobStore:
    """`BlobStore` over Supabase Storage (T065). `client`, `sleep` and `now` are for tests.

    `inputs` writes upsert; `reports` writes never do, so Storage itself refuses an existing
    path (409 → `UniqueViolation`, F-09.R5). Supabase fixes signed upload URLs at 2 hours, so
    `signed_upload_url` ignores `expires_s`; EP-07 enforces the upload window (C19).
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = clock.now,
    ) -> None:
        self._settings = settings
        self._client_obj = client
        self._sleep = sleep
        self._now = now

    def __repr__(self) -> str:
        return f"SupabaseBlobStore(url={self._settings.SUPABASE_URL!r})"

    def _storage(self) -> Any:
        if self._client_obj is None:
            from app.core.supabase_repo import create_service_client

            self._client_obj = create_service_client(self._settings)
        return self._client_obj.storage

    def _send(self, fn: Callable[[], T]) -> T:
        from app.core.supabase_repo import ATTEMPTS, SERVICE, WAITS_S, supabase_cause
        from app.services.runlog import retrying

        retry = retrying(SERVICE, ATTEMPTS, WAITS_S, cause_of=supabase_cause, sleep=self._sleep)
        return retry(fn)

    def _bucket(self, bucket: str) -> Any:
        _check_bucket(bucket)
        return self._storage().from_(bucket)

    def put(self, bucket: str, path: str, data: bytes, content_type: str) -> None:
        options = {
            "content-type": content_type,
            "upsert": "false" if bucket == REPORTS_BUCKET else "true",
        }
        files = self._bucket(bucket)
        try:
            self._send(lambda: files.upload(path, bytes(data), file_options=options))
        except Exception as exc:
            if _status(exc) == "409":
                raise UniqueViolation(f"{bucket}/{path}") from None
            raise

    def get(self, bucket: str, path: str) -> bytes:
        files = self._bucket(bucket)
        try:
            return bytes(self._send(lambda: files.download(path)))
        except Exception as exc:
            if _status(exc) in ("400", "404") or getattr(exc, "code", None) == "not_found":
                raise NotFound("blob", f"{bucket}/{path}") from None
            raise

    def exists(self, bucket: str, path: str) -> bool:
        files = self._bucket(bucket)
        return bool(self._send(lambda: files.exists(path)))

    def delete(self, bucket: str, path: str) -> None:
        """Removes an `inputs` object (no-op if absent); `reports` objects are permanent."""
        _check_bucket(bucket)
        if bucket == REPORTS_BUCKET:
            raise AppendOnlyViolation(f"{bucket}/{path}")
        files = self._bucket(bucket)
        self._send(lambda: files.remove([path]))

    def bucket_ok(self, bucket: str) -> bool:
        if bucket not in BUCKETS:
            return False
        try:
            self._storage().get_bucket(bucket)
        except Exception as exc:  # EP-01 reports down; never raises
            logger.warning("bucket %s not reachable: %s", bucket, type(exc).__name__)
            return False
        return True

    def signed_upload_url(self, bucket: str, path: str, expires_s: int = 600) -> str:
        from storage3.types import CreateSignedUploadUrlOptions

        files = self._bucket(bucket)
        upsert = CreateSignedUploadUrlOptions(
            upsert="false" if bucket == REPORTS_BUCKET else "true"
        )
        signed = self._send(lambda: files.create_signed_upload_url(path, upsert))
        return str(signed["signed_url"])

    def signed_download_url(
        self, bucket: str, path: str, expires_s: int | None = None
    ) -> tuple[str, datetime]:
        """Valid `expires_s` seconds (default `DOWNLOAD_URL_S`, Section 10)."""
        seconds = self._settings.DOWNLOAD_URL_S if expires_s is None else expires_s
        files = self._bucket(bucket)
        expires_at = self._now() + timedelta(seconds=seconds)
        signed = self._send(lambda: files.create_signed_url(path, seconds))
        return str(signed["signedURL"]), expires_at


@lru_cache(maxsize=1)
def _supabase_blobstore() -> SupabaseBlobStore:
    return SupabaseBlobStore(get_settings())


@lru_cache(maxsize=1)
def shared_memory_blobstore() -> MemoryBlobStore:
    """The process-wide `MemoryBlobStore` behind `REPO_BACKEND=memory`; tests clear the cache."""
    return MemoryBlobStore()


def get_blobstore() -> BlobStore:
    """FastAPI dependency (004 data-model §5): the store chosen by `REPO_BACKEND`."""
    if get_settings().REPO_BACKEND == "memory":
        return shared_memory_blobstore()
    return _supabase_blobstore()


if TYPE_CHECKING:  # static conformance check (mypy)
    _conforms: BlobStore = MemoryBlobStore()
    _conforms_supabase: BlobStore = SupabaseBlobStore(get_settings())
