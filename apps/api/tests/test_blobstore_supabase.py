"""T063/T066: the BlobStore contract tests (test_blobstore.py) against `SupabaseBlobStore`.

`integration` only (see tests/support/supabase_project.py). The contract tests take a fixture
named `memory_blobs`; here it is a `SupabaseBlobStore` behind a per-test path prefix, and every
object written is removed afterwards with the raw client (bypassing the reports rule).
"""

import uuid
from collections.abc import Iterator
from datetime import datetime

import httpx
import pytest

import tests.test_blobstore as contract
from app.core import clock
from app.core.storage import SupabaseBlobStore
from app.core.supabase_repo import create_service_client
from tests.support.supabase_project import guard_test_project, load_test_settings

pytestmark = pytest.mark.integration

for _name in (
    "test_put_get_roundtrip",
    "test_get_missing_raises_not_found",
    "test_reports_never_overwritten",
    "test_unknown_bucket_rejected",
    "test_blobstore_bucket_ok",
):
    globals()[_name] = getattr(contract, _name)


class PrefixedBlobs:
    """`BlobStore` that puts every path under `prefix/` and remembers what it wrote."""

    def __init__(self, store: SupabaseBlobStore) -> None:
        self.store = store
        self.prefix = f"contract-{uuid.uuid4().hex}"
        self.written: set[tuple[str, str]] = set()

    def _p(self, path: str) -> str:
        return f"{self.prefix}/{path}"

    def put(self, bucket: str, path: str, data: bytes, content_type: str) -> None:
        self.written.add((bucket, self._p(path)))
        self.store.put(bucket, self._p(path), data, content_type)

    def get(self, bucket: str, path: str) -> bytes:
        return self.store.get(bucket, self._p(path))

    def exists(self, bucket: str, path: str) -> bool:
        return self.store.exists(bucket, self._p(path))

    def delete(self, bucket: str, path: str) -> None:
        self.store.delete(bucket, self._p(path))

    def signed_upload_url(self, bucket: str, path: str, expires_s: int = 600) -> str:
        self.written.add((bucket, self._p(path)))
        return self.store.signed_upload_url(bucket, self._p(path), expires_s)

    def signed_download_url(
        self, bucket: str, path: str, expires_s: int | None = None
    ) -> tuple[str, datetime]:
        return self.store.signed_download_url(bucket, self._p(path), expires_s)

    def bucket_ok(self, bucket: str) -> bool:
        return self.store.bucket_ok(bucket)


@pytest.fixture
def memory_blobs() -> Iterator[PrefixedBlobs]:
    """Overrides conftest's `memory_blobs` for the tests collected in this module."""
    settings = load_test_settings()
    client = create_service_client(settings)
    guard_test_project(client)
    blobs = PrefixedBlobs(SupabaseBlobStore(settings, client=client))
    yield blobs
    for bucket in ("inputs", "reports"):
        paths = [p for b, p in blobs.written if b == bucket]
        if paths:
            client.storage.from_(bucket).remove(paths)


def test_supabase_signed_urls_round_trip(memory_blobs: PrefixedBlobs) -> None:
    """Signed upload then signed download through Supabase; expiry from DOWNLOAD_URL_S."""
    upload = memory_blobs.signed_upload_url("inputs", "r1/erp.csv")
    put = httpx.put(upload, content=b"a,b\n1,2\n", headers={"content-type": "text/csv"})
    assert put.status_code == 200, put.status_code
    assert memory_blobs.get("inputs", "r1/erp.csv") == b"a,b\n1,2\n"

    before = clock.now()
    url, expires_at = memory_blobs.signed_download_url("inputs", "r1/erp.csv")
    seconds = memory_blobs.store._settings.DOWNLOAD_URL_S
    assert 0 <= (expires_at - before).total_seconds() - seconds < 5
    assert httpx.get(url).content == b"a,b\n1,2\n"
