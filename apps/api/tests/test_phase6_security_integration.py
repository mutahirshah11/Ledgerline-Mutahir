"""007-ship FR-003..FR-007: integration tests proving Ledgerline's security guarantees hold on
a real Supabase project (schema shape, append-only audit, anon-key RLS denial, storage
isolation, signed-link expiry). Needs apps/api/.env.test (the ledgerline-test project);
tests/support/supabase_project.py refuses to run against the dev/prod project. FR-009: every
record created here is tagged actor/path "integration-test" and storage objects are cleaned up
(audit_log rows are append-only by design and are left in place)."""

import time
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from postgrest.exceptions import APIError

from app.core.storage import INPUTS_BUCKET, REPORTS_BUCKET, SupabaseBlobStore
from app.core.supabase_repo import create_service_client
from tests.support.supabase_project import create_anon_client, load_test_project

pytestmark = pytest.mark.integration

TABLES = (
    "allowed_users",
    "runs",
    "source_files",
    "page_manifest",
    "extractions",
    "groups",
    "source_rows",
    "normalisation_log",
    "control_flags",
    "review_items",
    "proofs",
    "notifications",
    "run_events",
    "audit_log",
)


@pytest.fixture(scope="module")
def supabase_project():
    return load_test_project()


@pytest.fixture(scope="module")
def service_client(supabase_project):
    return create_service_client(supabase_project.settings)


@pytest.fixture(scope="module")
def anon_client(supabase_project):
    return create_anon_client(supabase_project)


@pytest.fixture
def blobstore(service_client, supabase_project):
    return SupabaseBlobStore(supabase_project.settings, client=service_client)


# --------------------------------------------------------------------------------------------
# FR-003: schema shape, checked before anything else in this module runs
# --------------------------------------------------------------------------------------------


def test_FR003_schema_matches_target_shape(service_client):
    report = service_client.rpc("ledgerline_schema_report", {}).execute().data
    assert report == {
        "table_count": 14,
        "rls_enabled_count": 14,
        "policy_count": 0,
        "private_bucket_count": 2,
        "append_only_trigger_exists": True,
        "replace_reconciliation_exists": True,
    }


# --------------------------------------------------------------------------------------------
# FR-005 (F-11.AC3): audit_log is append-only even for the service-role key
# --------------------------------------------------------------------------------------------


def _insert_audit_probe(service_client, action: str) -> int:
    row = (
        service_client.table("audit_log")
        .insert(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "actor": "integration-test",
                "action": action,
                "entity": "integration-test",
                "entity_id": str(uuid.uuid4()),
                "channel": "SYSTEM",
            }
        )
        .execute()
        .data
    )
    return row[0]["id"]


def test_FR005_audit_log_update_denied(service_client):
    row_id = _insert_audit_probe(service_client, "FR005_UPDATE_PROBE")

    with pytest.raises(APIError) as exc_info:
        service_client.table("audit_log").update({"action": "TAMPERED"}).eq(
            "id", row_id
        ).execute()
    assert "append-only" in (exc_info.value.message or "")

    unchanged = service_client.table("audit_log").select("action").eq("id", row_id).execute()
    assert unchanged.data == [{"action": "FR005_UPDATE_PROBE"}]


def test_FR005_audit_log_delete_denied(service_client):
    row_id = _insert_audit_probe(service_client, "FR005_DELETE_PROBE")

    with pytest.raises(APIError) as exc_info:
        service_client.table("audit_log").delete().eq("id", row_id).execute()
    assert "append-only" in (exc_info.value.message or "")

    still_there = service_client.table("audit_log").select("id").eq("id", row_id).execute()
    assert still_there.data == [{"id": row_id}]


# --------------------------------------------------------------------------------------------
# FR-006 (F-12.AC4): the anon key is denied on every table, for both reads and writes
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("table", TABLES)
def test_FR006_anon_select_denied(anon_client, table):
    with pytest.raises(APIError) as exc_info:
        anon_client.table(table).select("*").execute()
    assert exc_info.value.code == "42501"


@pytest.mark.parametrize("table", TABLES)
def test_FR006_anon_insert_denied(anon_client, table):
    with pytest.raises(APIError) as exc_info:
        anon_client.table(table).insert({}).execute()
    assert exc_info.value.code == "42501"


# --------------------------------------------------------------------------------------------
# FR-004 (F-01.AC6): storage objects are unreachable except via a service-issued signed link
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("bucket", [INPUTS_BUCKET, REPORTS_BUCKET])
def test_FR004_storage_isolation(blobstore, anon_client, supabase_project, bucket):
    path = f"integration-test/{uuid.uuid4()}.txt"
    data = b"FR-004 isolation probe"
    blobstore.put(bucket, path, data, "text/plain")
    try:
        public_url = (
            f"{supabase_project.settings.SUPABASE_URL}/storage/v1/object/public/{bucket}/{path}"
        )
        assert httpx.get(public_url, timeout=10).status_code in (400, 403, 404)

        with pytest.raises(Exception):
            anon_client.storage.from_(bucket).download(path)

        # no policy grants anon any row in storage.objects, so RLS hides it (empty, not an error)
        listing = anon_client.storage.from_(bucket).list("integration-test")
        assert all(entry.get("name") != path.rsplit("/", 1)[-1] for entry in listing)

        signed_url, _ = blobstore.signed_download_url(bucket, path, expires_s=30)
        signed_response = httpx.get(signed_url, timeout=10)
        assert signed_response.status_code == 200
        assert signed_response.content == data
    finally:
        if bucket == INPUTS_BUCKET:
            blobstore.delete(bucket, path)


# --------------------------------------------------------------------------------------------
# FR-007 (F-12.AC5): signed links expire; EP-16's default lifetime is checked offline
# (test_api_settings.py, test_F08_review.py) — these two prove the expiry is real.
# --------------------------------------------------------------------------------------------


def test_FR007_signed_link_expires_after_ttl(blobstore):
    path = f"integration-test/{uuid.uuid4()}.txt"
    blobstore.put(INPUTS_BUCKET, path, b"FR-007 expiry probe", "text/plain")
    try:
        signed_url, _ = blobstore.signed_download_url(INPUTS_BUCKET, path, expires_s=5)
        assert httpx.get(signed_url, timeout=10).status_code == 200
        time.sleep(10)
        assert httpx.get(signed_url, timeout=10).status_code in (400, 403)
    finally:
        blobstore.delete(INPUTS_BUCKET, path)


@pytest.mark.slow
def test_FR007_EP16_default_link_lifetime_expires(blobstore):
    """The real 300s EP-16 lifetime, allowing the spec's 30s clock-skew margin."""
    path = f"integration-test/{uuid.uuid4()}.txt"
    blobstore.put(INPUTS_BUCKET, path, b"FR-007 EP-16 lifetime probe", "text/plain")
    try:
        signed_url, _ = blobstore.signed_download_url(INPUTS_BUCKET, path)  # default 300s
        assert httpx.get(signed_url, timeout=10).status_code == 200
        time.sleep(305)
        assert httpx.get(signed_url, timeout=10).status_code in (400, 403)
    finally:
        blobstore.delete(INPUTS_BUCKET, path)
