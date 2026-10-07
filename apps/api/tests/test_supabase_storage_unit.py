"""SupabaseBlobStore mapping logic, offline (T065). A stand-in storage client records calls;
the real behaviour is checked under the `integration` marker (test_blobstore_supabase.py)."""

from datetime import timedelta
from typing import Any

import pytest
from storage3.exceptions import StorageApiError

from app.core.config import Settings
from app.core.errors import AppendOnlyViolation, NotFound, UniqueViolation
from app.core.storage import BlobStore, SupabaseBlobStore
from tests.factories import T0


class FakeBucket:
    def __init__(self, storage: "FakeStorage", bucket: str) -> None:
        self.storage, self.bucket = storage, bucket

    def __getattr__(self, name: str) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            self.storage.calls.append((self.bucket, name, args, kwargs))
            result = self.storage.results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result

        return call


class FakeStorage:
    def __init__(self, *results: Any) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, str, tuple[Any, ...], dict[str, Any]]] = []

    def from_(self, bucket: str) -> FakeBucket:
        return FakeBucket(self, bucket)

    def get_bucket(self, bucket: str) -> Any:
        return FakeBucket(self, bucket).get_bucket(bucket)


class FakeClient:
    def __init__(self, *results: Any) -> None:
        self.storage = FakeStorage(*results)


def _store(*results: Any) -> tuple[SupabaseBlobStore, FakeStorage]:
    client = FakeClient(*results)
    settings = Settings(_env_file=None, DOWNLOAD_URL_S=120)
    store = SupabaseBlobStore(settings, client=client, sleep=lambda _: None, now=lambda: T0)
    return store, client.storage


def test_inputs_upsert_reports_never_overwritten() -> None:
    dup = StorageApiError("The resource already exists", "Duplicate", 409)
    store, storage = _store({"Key": "k"}, dup)
    assert isinstance(store, BlobStore)
    store.put("inputs", "r1/erp.csv", b"x", "text/csv")
    _, _, _, kwargs = storage.calls[0]
    assert kwargs["file_options"] == {"content-type": "text/csv", "upsert": "true"}
    with pytest.raises(UniqueViolation):
        store.put("reports", "r1/report.html", b"<p/>", "text/html")
    assert storage.calls[1][3]["file_options"] == {"content-type": "text/html", "upsert": "false"}
    with pytest.raises(AppendOnlyViolation):
        store.delete("reports", "r1/report.html")
    with pytest.raises(ValueError):
        store.put("public", "a", b"x", "text/plain")
    assert len(storage.calls) == 2  # no request for the refused calls


def test_get_missing_is_not_found() -> None:
    missing = StorageApiError("Object not found", "not_found", 400)
    store, _ = _store(b"data", missing)
    assert store.get("inputs", "a.csv") == b"data"
    with pytest.raises(NotFound):
        store.get("inputs", "nope.csv")


def test_signed_download_url_uses_download_url_s() -> None:
    store, storage = _store({"signedURL": "https://x/sign/reports/a?token=t"})
    url, expires_at = store.signed_download_url("reports", "a")
    assert url == "https://x/sign/reports/a?token=t"
    assert expires_at == T0 + timedelta(seconds=120)
    assert storage.calls[0][1:3] == ("create_signed_url", ("a", 120))


def test_bucket_ok_never_raises() -> None:
    store, _ = _store(object(), StorageApiError("Bucket not found", "not_found", 404))
    assert store.bucket_ok("inputs") is True
    assert store.bucket_ok("reports") is False
    assert store.bucket_ok("missing") is False  # unknown name: no request
    assert SupabaseBlobStore(Settings(_env_file=None)).bucket_ok("inputs") is False
