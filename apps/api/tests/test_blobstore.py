"""BlobStore contract (contracts/repo-interface.md, BlobStore section) against MemoryBlobStore."""

import re
from collections.abc import Callable
from datetime import timedelta

import pytest

from app.core.errors import AppendOnlyViolation, NotFound, UniqueViolation
from app.core.storage import BlobStore, MemoryBlobStore
from tests.factories import T0


def test_put_get_roundtrip(memory_blobs: MemoryBlobStore) -> None:
    assert isinstance(memory_blobs, BlobStore)
    assert memory_blobs.exists("inputs", "r1/erp.csv") is False
    memory_blobs.put("inputs", "r1/erp.csv", b"a,b\n1,2\n", "text/csv")
    assert memory_blobs.exists("inputs", "r1/erp.csv") is True
    assert memory_blobs.get("inputs", "r1/erp.csv") == b"a,b\n1,2\n"
    assert memory_blobs.exists("reports", "r1/erp.csv") is False  # buckets are separate

    memory_blobs.put("inputs", "r1/erp.csv", b"new", "text/csv")  # inputs may be replaced
    assert memory_blobs.get("inputs", "r1/erp.csv") == b"new"

    memory_blobs.delete("inputs", "r1/erp.csv")
    assert memory_blobs.exists("inputs", "r1/erp.csv") is False


def test_get_missing_raises_not_found(memory_blobs: MemoryBlobStore) -> None:
    with pytest.raises(NotFound):
        memory_blobs.get("inputs", "nope.csv")
    memory_blobs.put("inputs", "a.csv", b"x", "text/csv")
    with pytest.raises(NotFound):
        memory_blobs.get("reports", "a.csv")


def test_reports_never_overwritten(memory_blobs: MemoryBlobStore) -> None:
    memory_blobs.put("reports", "r1/report.html", b"<p>v1</p>", "text/html")
    with pytest.raises(UniqueViolation):
        memory_blobs.put("reports", "r1/report.html", b"<p>v2</p>", "text/html")
    assert memory_blobs.get("reports", "r1/report.html") == b"<p>v1</p>"
    # delete-then-put would be an overwrite, so reports cannot be deleted either
    with pytest.raises(AppendOnlyViolation):
        memory_blobs.delete("reports", "r1/report.html")
    assert memory_blobs.get("reports", "r1/report.html") == b"<p>v1</p>"


def test_unknown_bucket_rejected(memory_blobs: MemoryBlobStore) -> None:
    calls: list[Callable[[], object]] = [
        lambda: memory_blobs.put("public", "a", b"x", "text/plain"),
        lambda: memory_blobs.get("public", "a"),
        lambda: memory_blobs.exists("public", "a"),
        lambda: memory_blobs.delete("public", "a"),
        lambda: memory_blobs.signed_upload_url("public", "a"),
        lambda: memory_blobs.signed_download_url("public", "a"),
    ]
    for call in calls:
        with pytest.raises(ValueError):
            call()


def test_signed_url_shapes(memory_blobs: MemoryBlobStore) -> None:
    exp_up = int((T0 + timedelta(seconds=600)).timestamp())
    assert memory_blobs.signed_upload_url("inputs", "r1/erp.csv") == (
        f"memory://inputs/r1/erp.csv?exp={exp_up}"
    )

    url, expires_at = memory_blobs.signed_download_url("reports", "r1/report.html")
    assert expires_at == T0 + timedelta(seconds=300)
    assert expires_at.tzinfo is not None
    assert url == f"memory://reports/r1/report.html?exp={int(expires_at.timestamp())}"

    url2, expires2 = memory_blobs.signed_download_url("reports", "r1/report.html", expires_s=60)
    assert expires2 == T0 + timedelta(seconds=60)
    assert re.fullmatch(r"memory://reports/r1/report\.html\?exp=\d+", url2)
