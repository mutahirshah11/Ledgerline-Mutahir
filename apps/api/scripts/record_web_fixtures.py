"""Record the web app's screen-test fixtures from the real API (006 T007, research R13).

Runs the 2026-09 pack (`data/kestrel-2026-09`) through the real FastAPI app (`create_app`) with
a `MemoryRepo`, a `MemoryBlobStore` whose signed links point at `http://storage.test/...`, the
committed page reads (`supabase/seed/extractions`, replayed, never the model), a quiet notifier
(no network) and a scenario clock that starts at 2026-10-15 08:30 Asia/Karachi and moves 3 s
per POST. Every call goes through `TestClient` with a real ES256 bearer token per role, checked
against a JWKS made at run time (no key material is committed).

Writes `apps/web/e2e/fixtures/pack/<fixture>.json` (the response body as received) and
`index.json`: the runs by label, every fixture's endpoint, method, path template, concrete
path, query, status and role, and the screen scenarios (`pre-review`, `post-review`, ...) the
web mock (`apps/web/e2e/support/mock-api.ts`) serves. Output is deterministic: every UUID is
replaced by a stable fake one in order of first appearance, keys are sorted, 2-space indent,
trailing newline. Regenerate after any API change, then commit the diff:

    uv run python -m scripts.record_web_fixtures            # from apps/api

Worlds (separate repos, because a pack may be finalised once per repo):

- `main`: `failed-extract` run (page 2 times out, with RETRY events), the `main` run (pre-review,
  every review action, post-review, finalised and sent), `failed-proof` (PROOF_DROP_ROW),
  `duplicate` (409 DUPLICATE_RUN on confirm) and `validation-fail` (month 2026-08: the store
  sheet has no `Aug-2026` worksheet).
- `summary`: an empty run list, then one pack run finalised while Telegram is down.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from jwt.algorithms import ECAlgorithm

from app.core import clock
from app.core.auth import JwksCache, get_jwks_cache
from app.core.config import Settings, get_settings
from app.core.db import get_repo
from app.core.deps import get_extract_sleep, get_model_client, get_notifier
from app.core.memory_repo import MemoryRepo
from app.core.storage import INPUTS_BUCKET, MemoryBlobStore, get_blobstore
from app.domain.enums import Role
from app.domain.models import AllowedUser
from app.main import create_app
from app.pipeline.extract import load_cached_extractions
from app.pipeline.model_client import ReplayModelClient
from app.services import runlog
from app.services.runlog import OnRetry

SCRIPTS_DIR = Path(__file__).resolve().parent
API_DIR = SCRIPTS_DIR.parent
REPO_ROOT = API_DIR.parent.parent
PACK_DIR = REPO_ROOT / "data" / "kestrel-2026-09"
CACHE_DIR = REPO_ROOT / "supabase" / "seed" / "extractions"
OUTPUT_DIR = REPO_ROOT / "apps" / "web" / "e2e" / "fixtures" / "pack"

API = "/api/v1"
SCENARIO_START = datetime(2026, 10, 15, 8, 30, tzinfo=clock.KARACHI)
POST_STEP = timedelta(seconds=3)
WEB_ORIGIN = "https://ledgerline.test"
SUPABASE_URL = "http://supabase.test"
STORAGE_HOST = "http://storage.test"
FAKE_UUID = "00000000-0000-4000-8000-{n:012x}"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

USERS = {
    "CONTROLLER": AllowedUser(
        email="controller@kestrel.example", display_name="Controller (Demo)", role=Role.CONTROLLER
    ),
    "REVIEWER": AllowedUser(
        email="reviewer@kestrel.example", display_name="Reviewer (Demo)", role=Role.REVIEWER
    ),
    "OPS": AllowedUser(email="ops@kestrel.example", display_name="Ops (Demo)", role=Role.OPS),
}
"""The seeded allowlist (`supabase/seed/allowed_users.sql`, C13)."""
STRANGER = "stranger@kestrel.example"
PACK_FILES = (
    ("t4_register_page1.jpg", "REGISTER", 1),
    ("t4_register_page2.jpg", "REGISTER", 2),
    ("t4_erp_grn.csv", "ERP", None),
    ("t4_store_sheet.xlsx", "SHEET", None),
)

QTY_4419 = "CELL:REG-p2-r05:qty"
QTY_4428 = "CELL:REG-p2-r14:qty"


def _settings_env() -> dict[str, str]:
    """Hermetic settings for the recording. Fake secrets are built at run time (gitleaks)."""
    fake = "-".join(("fake", "value", "for", "fixtures"))
    return {
        "REPO_BACKEND": "memory",
        "SUPABASE_URL": SUPABASE_URL,
        "OPENAI_API_KEY": fake,
        "TELEGRAM_BOT_TOKEN": fake,
        "TELEGRAM_CONTROLLER_CHAT_ID": "controller-chat",
        "TELEGRAM_OPS_CHAT_ID": "ops-chat",
        "ALLOW_FAULTS": "true",
        "EXTRACT_PRICE_INPUT_PER_MTOK": "0.20",
        "EXTRACT_PRICE_OUTPUT_PER_MTOK": "1.20",
        "TELEGRAM_BACKOFF_S": "[0]",
        "EXTRACT_BACKOFF_S": "[0]",
        "CORS_ORIGINS": WEB_ORIGIN,
        "APP_BASE_URL": WEB_ORIGIN,
    }


# --- process wiring ---------------------------------------------------------------------


class ScenarioClock:
    """The instant `app.core.clock` reports; `advance` moves it (one step per POST)."""

    def __init__(self, start: datetime) -> None:
        self.at = start.astimezone(UTC)

    def __call__(self) -> datetime:
        return self.at

    def advance(self, by: timedelta = POST_STEP) -> None:
        self.at += by


class StepTimer:
    """Stands in for `time.monotonic` in `runlog.stage_step`: 0.4 s more on every reading, so
    each run event's `duration_ms` depends only on the code path, never on the machine."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        self.t += 0.4
        return self.t


@contextmanager
def recording_process(scenario: ScenarioClock) -> Iterator[None]:
    """Settings from `_settings_env` only (no OS env, no .env), `app.core.clock` on the
    scenario clock and `stage_step` durations from a `StepTimer`; all undone on exit, so a
    test may call `record` in process."""
    saved_env = dict(os.environ)
    saved_env_file = Settings.model_config.get("env_file")
    saved_real_now = clock._real_utc_now
    step = runlog.stage_step.__wrapped__  # type: ignore[attr-defined]  # under @contextmanager
    saved_step_defaults = step.__kwdefaults__
    try:
        for name in Settings.model_fields:
            os.environ.pop(name, None)
        os.environ.update(_settings_env())
        Settings.model_config["env_file"] = None
        get_settings.cache_clear()
        clock._real_utc_now = scenario
        step.__kwdefaults__ = {**saved_step_defaults, "timer": StepTimer()}
        yield
    finally:
        step.__kwdefaults__ = saved_step_defaults
        clock._real_utc_now = saved_real_now
        os.environ.clear()
        os.environ.update(saved_env)
        Settings.model_config["env_file"] = saved_env_file
        get_settings.cache_clear()


@contextmanager
def prices_unset() -> Iterator[None]:
    """The read cost price settings unset (EP-05 `read_cost` estimate and actual are null)."""
    names = ("EXTRACT_PRICE_INPUT_PER_MTOK", "EXTRACT_PRICE_OUTPUT_PER_MTOK")
    saved = {name: os.environ.pop(name) for name in names}
    get_settings.cache_clear()
    try:
        yield
    finally:
        os.environ.update(saved)
        get_settings.cache_clear()


class StorageTestBlobStore(MemoryBlobStore):
    """`MemoryBlobStore` whose signed links are `http://storage.test/{bucket}/{path}?exp=...`
    (the web mock answers them; no image bytes are committed)."""

    def _url(self, bucket: str, path: str, expires_at: datetime) -> str:
        super()._url(bucket, path, expires_at)  # bucket check
        return f"{STORAGE_HOST}/{bucket}/{path}?exp={int(expires_at.timestamp())}"


class QuietNotifier:
    """Delivers nothing and always succeeds (`Notifier`); ids 1, 2, ... like Telegram's."""

    def __init__(self) -> None:
        self._last_id = 0

    def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        on_retry: OnRetry | None = None,
        at_most_once: bool = False,
    ) -> int:
        self._last_id += 1
        return self._last_id

    def send_document(
        self,
        chat_id: str,
        filename: str,
        content: bytes,
        caption: str | None = None,
        *,
        on_retry: OnRetry | None = None,
    ) -> int:
        self._last_id += 1
        return self._last_id


class Tokens:
    """ES256 bearer tokens like Supabase's, signed by a key made now, with its JWKS."""

    KID = "record-web-fixtures"

    def __init__(self) -> None:
        self._key = ec.generate_private_key(ec.SECP256R1())
        jwk: dict[str, Any] = ECAlgorithm.to_jwk(self._key.public_key(), as_dict=True)
        self.jwks = {"keys": [jwk | {"kid": self.KID, "alg": "ES256", "use": "sig"}]}

    def fetch(self, url: str = "") -> dict[str, Any]:
        return self.jwks

    def header(self, email: str) -> dict[str, str]:
        claims = {
            "iss": f"{SUPABASE_URL}/auth/v1",
            "aud": "authenticated",
            "exp": datetime(2099, 1, 1, tzinfo=UTC),
            "sub": "record-web-fixtures",
            "email": email,
        }
        token = jwt.encode(claims, self._key, algorithm="ES256", headers={"kid": self.KID})
        return {"Authorization": f"Bearer {token}"}


# --- recording --------------------------------------------------------------------------


@dataclass
class Fixture:
    name: str
    endpoint: str
    method: str
    template: str
    path: str
    query: dict[str, str]
    status: int
    role: str | None
    body: Any


@dataclass
class Recording:
    stop_after: str | None = None
    fixtures: list[Fixture] = field(default_factory=list)
    runs: dict[str, str] = field(default_factory=dict)
    review_items: dict[str, str] = field(default_factory=dict)
    source_rows: dict[str, str] = field(default_factory=dict)

    def add(self, fixture: Fixture) -> None:
        if any(f.name == fixture.name for f in self.fixtures):
            raise ValueError(f"fixture recorded twice: {fixture.name}")
        self.fixtures.append(fixture)
        if fixture.name == self.stop_after:
            raise _Stop


class _Stop(Exception):
    """Ends a partial recording (`record(stop_after=...)`)."""


class World:
    """One app over its own MemoryRepo / blob store / notifier, recording into `rec`."""

    def __init__(self, rec: Recording, scenario: ScenarioClock, tokens: Tokens) -> None:
        self.rec = rec
        self.scenario = scenario
        self.tokens = tokens
        self.repo = MemoryRepo()
        self.blobs = StorageTestBlobStore(now=clock.now)
        for user in USERS.values():
            self.repo.upsert_allowed_user(user)
        settings = get_settings()
        replay = ReplayModelClient(
            CACHE_DIR, model=settings.EXTRACT_MODEL, prompt_version=settings.PROMPT_VERSION
        )
        jwks = JwksCache(tokens.fetch)
        notifier = QuietNotifier()
        app = create_app()
        app.dependency_overrides[get_repo] = lambda: self.repo
        app.dependency_overrides[get_blobstore] = lambda: self.blobs
        app.dependency_overrides[get_notifier] = lambda: notifier
        app.dependency_overrides[get_jwks_cache] = lambda: jwks
        app.dependency_overrides[get_model_client] = lambda: replay
        app.dependency_overrides[get_extract_sleep] = lambda: _no_sleep
        self.client = TestClient(app, headers={"Origin": WEB_ORIGIN})

    def call(
        self,
        method: str,
        template: str,
        *,
        endpoint: str,
        name: str | None = None,
        role: str | None = "CONTROLLER",
        email: str | None = None,
        json_body: Any = None,
        query: dict[str, str] | None = None,
        expect: int = 200,
        **path_params: Any,
    ) -> Any:
        """Calls the API as `role` (or `email`; neither: no token); asserts `expect`; records
        the body as fixture `name` when given; returns the body."""
        if method != "GET":
            self.scenario.advance()
        path = template.format(**path_params)
        who = email or (USERS[role].email if role else None)
        headers = self.tokens.header(who) if who else {}
        resp = self.client.request(
            method, API + path, json=json_body, params=query, headers=headers
        )
        if resp.status_code != expect:
            raise AssertionError(f"{method} {path}: {resp.status_code} {resp.text[:2000]}")
        body = resp.json()
        if name is not None:
            self.rec.add(
                Fixture(
                    name=name,
                    endpoint=endpoint,
                    method=method,
                    template=template,
                    path=path,
                    query=dict(query or {}),
                    status=resp.status_code,
                    role=role if email is None else None,
                    body=body,
                )
            )
        return body

    # --- pack steps ---

    def create(self, month: str = "2026-09", name: str | None = None) -> str:
        body = self.call(
            "POST", "/runs", endpoint="EP-03", name=name, json_body={"month": month}, expect=201
        )
        run_id: str = body["id"]
        return run_id

    def upload(self, run_id: str, *, name: str | None = None) -> None:
        declared = []
        data: dict[str, bytes] = {}
        for filename, source, page_no in PACK_FILES:
            data[filename] = (PACK_DIR / filename).read_bytes()
            entry: dict[str, Any] = {
                "source": source,
                "filename": filename,
                "size_bytes": len(data[filename]),
                "sha256": hashlib.sha256(data[filename]).hexdigest(),
            }
            if page_no is not None:
                entry["page_no"] = page_no
            declared.append(entry)
        body = self.call(
            "POST",
            "/runs/{id}/uploads",
            endpoint="EP-06",
            name=name,
            json_body={"files": declared},
            id=run_id,
        )
        for slot in body["uploads"]:  # the browser's PUT to each signed link
            self.blobs.put(
                INPUTS_BUCKET,
                slot["storage_path"],
                data[slot["filename"]],
                "application/octet-stream",
            )

    def confirm(self, run_id: str, *, name: str | None = None, expect: int = 200) -> Any:
        return self.call(
            "POST",
            "/runs/{id}/uploads/confirm",
            endpoint="EP-07",
            name=name,
            expect=expect,
            id=run_id,
        )

    def validate(self, run_id: str, *, name: str | None = None, expect: int = 200) -> Any:
        return self.call(
            "POST", "/runs/{id}/validate", endpoint="EP-08", name=name, expect=expect, id=run_id
        )

    def extract(
        self,
        run_id: str,
        page_no: int,
        *,
        name: str | None = None,
        fault: str | None = None,
        expect: int = 200,
    ) -> Any:
        return self.call(
            "POST",
            "/runs/{id}/pages/{page_no}/extract",
            endpoint="EP-09",
            name=name,
            query={"fault": fault} if fault else None,
            expect=expect,
            id=run_id,
            page_no=page_no,
        )

    def reconcile(self, run_id: str, *, name: str | None = None, fault: str | None = None) -> Any:
        return self.call(
            "POST",
            "/runs/{id}/reconcile",
            endpoint="EP-10",
            name=name,
            query={"fault": fault} if fault else None,
            id=run_id,
        )

    def run_view(self, run_id: str, name: str) -> Any:
        return self.call("GET", "/runs/{id}", endpoint="EP-05", name=name, id=run_id)

    def review_items(
        self, run_id: str, name: str | None, status: str | None = "OPEN", role: str = "REVIEWER"
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = self.call(
            "GET",
            "/runs/{id}/review-items",
            endpoint="EP-14",
            name=name,
            role=role,
            query={"status": status} if status else None,
            id=run_id,
        )
        return items

    def resolve(
        self,
        item_id: str,
        body: dict[str, Any],
        *,
        name: str | None,
        role: str = "REVIEWER",
        expect: int = 200,
        query: dict[str, str] | None = None,
    ) -> Any:
        return self.call(
            "POST",
            "/review-items/{item_id}/resolve",
            endpoint="EP-15",
            name=name,
            role=role,
            json_body=body,
            query=query,
            expect=expect,
            item_id=item_id,
        )

    def finalise(
        self, run_id: str, *, name: str, expect: int = 200, fault: str | None = None
    ) -> Any:
        return self.call(
            "POST",
            "/runs/{id}/finalise",
            endpoint="EP-17",
            name=name,
            json_body={"confirm": True},
            query={"fault": fault} if fault else None,
            expect=expect,
            id=run_id,
        )


def _no_sleep(_: float) -> None:
    return None


def _decision(item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """A realistic reviewer decision per open item (blueprint Section 8 "after review": 4419
    accepted as 66, 4428 entered as 250 from the ERP and sheet, flags acknowledged)."""
    key, kind, grn = item["item_key"], item["kind"], item["grn_no"]
    if key == QTY_4419:
        return "accept-read-4419", {"action": "ACCEPT_READ"}
    if key == QTY_4428:
        return "enter-value-4428", {"action": "ENTER_VALUE", "value": "250"}
    if kind == "FLAG_ACK":
        reason = item["reason"].lower().replace("_", "-")
        note = (
            "Checked with stores; explained." if grn is None else f"Checked GRN {grn} with stores."
        )
        return f"acknowledge-{reason}-{grn or key.rsplit(':', 1)[-1].lower()}", {
            "action": "ACKNOWLEDGE",
            "note": note,
        }
    a, b = item.get("read_a") or {}, item.get("read_b") or {}
    if kind == "CELL" and a.get("value") is not None and a.get("value") == b.get("value"):
        return f"accept-read-{grn}-{item['field']}", {"action": "ACCEPT_READ"}
    return f"cannot-determine-{grn}-{item['field']}", {
        "action": "CANNOT_DETERMINE",
        "note": "Not clear on the page; asked stores to recount.",
    }


def _group_rows(groups: list[dict[str, Any]], grn: int, source: str) -> list[str]:
    for group in groups:
        if group["group"]["grn_no"] == grn:
            return [row["source_row_id"] for row in group["rows"].get(source, [])]
    raise AssertionError(f"no group for GRN {grn}")


# --- the worlds -------------------------------------------------------------------------


def record_main(world: World) -> None:
    w, rec = world, world.rec

    w.call("GET", "/health", endpoint="EP-01", name="ep01-health-ok", role=None)
    for role in USERS:
        w.call("GET", "/me", endpoint="EP-02", name=f"ep02-me-{role.lower()}", role=role)
    w.call("GET", "/me", endpoint="EP-02", name="ep02-not-allowed", email=STRANGER, expect=403)
    w.call("GET", "/me", endpoint="EP-02", name="ep02-unauthenticated", role=None, expect=401)

    # failed-extract: nothing cached yet; page 1 is read, page 2 times out after its retries.
    failed = w.create()
    rec.runs["failed-extract"] = failed
    w.upload(failed)
    w.confirm(failed, name="ep07-new-pages")
    w.validate(failed)
    w.extract(failed, 1, name="ep09-new-read-page-1")
    w.extract(failed, 2, name="ep09-upstream-failed", fault="EXTRACT_TIMEOUT", expect=502)
    w.run_view(failed, "ep05-failed-extract")
    w.call(
        "GET", "/runs/{id}/events", endpoint="EP-19", name="ep19-events-failed-extract", id=failed
    )
    w.call("GET", "/runs/{id}/audit", endpoint="EP-20", name="ep20-audit-failed-extract", id=failed)

    # main: page 1 is cached now, page 2 is read afresh (replayed).
    main = w.create(name="ep03-create-run")
    rec.runs["main"] = main
    w.run_view(main, "ep05-created")
    w.upload(main, name="ep06-uploads")
    w.confirm(main, name="ep07-known-pages")
    w.run_view(main, "ep05-uploaded")
    w.validate(main, name="ep08-pass")
    w.extract(main, 1, name="ep09-cached-page-1")
    w.run_view(main, "ep05-extracting")
    w.extract(main, 2, name="ep09-new-read-page-2")
    w.reconcile(main, name="ep10-reconcile-pass")
    w.run_view(main, "ep05-pre-review")
    with prices_unset():
        w.run_view(main, "ep05-pre-review-no-prices")
    groups = w.call("GET", "/runs/{id}/groups", endpoint="EP-11", name="ep11-groups", id=main)
    w.call(
        "GET",
        "/runs/{id}/groups",
        endpoint="EP-11",
        name="ep11-groups-mismatched",
        query={"outcome": "MISMATCHED"},
        id=main,
    )
    w.call("GET", "/runs/{id}/flags", endpoint="EP-12", name="ep12-flags", id=main)
    w.call("GET", "/runs/{id}/proof", endpoint="EP-13", name="ep13-proof-pass", id=main)
    w.call("GET", "/runs/{id}/normalisation-log", endpoint="EP-21", name="ep21-norm-log", id=main)
    (sheet_4416,) = _group_rows(groups, 4416, "SHEET")
    rec.source_rows["4416-sheet"] = sheet_4416
    w.call(
        "GET",
        "/runs/{id}/normalisation-log",
        endpoint="EP-21",
        name="ep21-norm-log-4416-sheet",
        query={"source_row_id": sheet_4416},
        id=main,
    )
    for page in (1, 2):
        w.call(
            "GET",
            "/runs/{id}/pages/{page_no}/image",
            endpoint="EP-16",
            name=f"ep16-page-{page}",
            role="REVIEWER",
            id=main,
            page_no=page,
        )
    w.call(
        "GET", "/runs/{id}/report", endpoint="EP-18", name="ep18-not-finalised", expect=409, id=main
    )
    w.call("GET", "/runs/{id}/events", endpoint="EP-19", name="ep19-events-pre-review", id=main)
    w.call("GET", "/runs/{id}/audit", endpoint="EP-20", name="ep20-audit-pre-review", id=main)

    # failed-proof: both pages cached; the proof drops a row.
    proof = w.create()
    rec.runs["failed-proof"] = proof
    w.upload(proof)
    w.confirm(proof)
    w.validate(proof)
    w.extract(proof, 1)
    w.extract(proof, 2)
    w.reconcile(proof, name="ep10-reconcile-proof-fail", fault="PROOF_DROP_ROW")
    w.run_view(proof, "ep05-failed-proof")
    w.call("GET", "/runs/{id}/proof", endpoint="EP-13", name="ep13-proof-fail", id=proof)
    w.finalise(proof, name="ep17-not-ready", expect=409)

    items = w.review_items(main, "ep14-open-pre-review")
    by_key = {item["item_key"]: item["id"] for item in items}
    rec.review_items.update({item["item_key"]: item["id"] for item in items})
    w.resolve(
        by_key[QTY_4428], {"action": "ACCEPT_READ"}, name="ep15-422-accept-no-read", expect=422
    )
    w.resolve(
        by_key[QTY_4428],
        {"action": "ENTER_VALUE", "value": "abc"},
        name="ep15-422-bad-qty",
        expect=422,
    )
    w.resolve(
        by_key[QTY_4419],
        {"action": "ACCEPT_READ"},
        name="ep15-forbidden-ops",
        role="OPS",
        expect=403,
    )
    remaining = items
    step = 0
    while remaining:
        item = remaining[0]
        label, body = _decision(item)
        w.resolve(item["id"], body, name=f"ep15-{label}")
        step += 1
        remaining = w.review_items(main, f"ep14-open-after-{step}")
    w.resolve(
        by_key[QTY_4419],
        {"action": "ENTER_VALUE", "value": "60"},
        name="ep15-409-already-resolved",
        expect=409,
    )
    w.run_view(main, "ep05-post-review")
    w.review_items(main, "ep14-all-post-review", status=None)
    w.call("GET", "/runs/{id}/groups", endpoint="EP-11", name="ep11-groups-post-review", id=main)
    w.call("GET", "/runs/{id}/audit", endpoint="EP-20", name="ep20-audit-post-review", id=main)

    w.finalise(main, name="ep17-sent")
    w.run_view(main, "ep05-finalised")
    w.call("GET", "/runs/{id}/report", endpoint="EP-18", name="ep18-report-links", id=main)
    w.call("GET", "/runs/{id}/events", endpoint="EP-19", name="ep19-events-finalised", id=main)
    w.call("GET", "/runs/{id}/audit", endpoint="EP-20", name="ep20-audit-finalised", id=main)
    w.review_items(main, "ep14-open-finalised")

    # duplicate: the same pack again after `main` was finalised.
    dup = w.create()
    rec.runs["duplicate"] = dup
    w.upload(dup)
    w.confirm(dup, name="ep07-duplicate-run", expect=409)

    # validation-fail: month 2026-08, so the store sheet has no "Aug-2026" worksheet.
    bad = w.create(month="2026-08")
    rec.runs["validation-fail"] = bad
    w.upload(bad)
    w.confirm(bad)
    w.validate(bad, name="ep08-missing-worksheet", expect=422)
    w.run_view(bad, "ep05-validation-fail")

    w.call("GET", "/runs", endpoint="EP-04", name="ep04-runs", role="REVIEWER")


def record_summary(world: World) -> None:
    w, rec = world, world.rec
    w.call("GET", "/runs", endpoint="EP-04", name="ep04-runs-empty")
    load_cached_extractions(w.repo, CACHE_DIR)
    run = w.create()
    rec.runs["summary-failed"] = run
    w.upload(run)
    w.confirm(run)
    w.validate(run)
    w.extract(run, 1)
    w.extract(run, 2)
    w.reconcile(run)
    items = w.review_items(run, None)
    (qty_4428,) = [i["id"] for i in items if i["item_key"] == QTY_4428]
    w.resolve(
        qty_4428,
        {"action": "CANNOT_DETERMINE", "note": "Ink blot; asked stores to recount."},
        name="ep15-cannot-determine-4428",
    )
    w.finalise(run, name="ep17-summary-failed", fault="TELEGRAM_DOWN")
    w.run_view(run, "ep05-summary-failed")
    w.call("GET", "/health", endpoint="EP-01", name="ep01-health-degraded", role=None, expect=503)


# --- scenarios served by the web mock -----------------------------------------------------

_RUN_READS = {
    "GET /runs/{id}/groups": ["ep11-groups", "ep11-groups-mismatched"],
    "GET /runs/{id}/flags": ["ep12-flags"],
    "GET /runs/{id}/normalisation-log": ["ep21-norm-log", "ep21-norm-log-4416-sheet"],
    "GET /runs/{id}/pages/{page_no}/image": ["ep16-page-1", "ep16-page-2"],
}
_COMMON = ["ep01-health-ok", "ep02-me-controller", "ep04-runs"]


def _scenarios() -> dict[str, list[str]]:
    """Scenario name -> fixture names, in priority order (the mock serves the best match)."""
    pre_review_resolves = [  # by review item id: each item gets its own decision
        "ep15-accept-read-4419",
        "ep15-enter-value-4428",
        "ep15-acknowledge-sequence-gap-4429",
        "ep15-acknowledge-backdated-entry-4430",
    ]
    run_reads = [name for names in _RUN_READS.values() for name in names]
    return {
        "pre-review": [
            *_COMMON,
            "ep05-pre-review",
            *run_reads,
            "ep13-proof-pass",
            "ep14-open-pre-review",
            *pre_review_resolves,
            "ep17-sent",
            "ep18-not-finalised",
            "ep19-events-pre-review",
            "ep20-audit-pre-review",
        ],
        "post-review": [
            *_COMMON,
            "ep05-post-review",
            "ep11-groups-post-review",
            *run_reads,
            "ep13-proof-pass",
            "ep14-all-post-review",
            "ep17-sent",
            "ep18-not-finalised",
            "ep19-events-pre-review",
            "ep20-audit-post-review",
        ],
        "finalised": [
            *_COMMON,
            "ep05-finalised",
            "ep11-groups-post-review",
            *run_reads,
            "ep13-proof-pass",
            "ep14-open-finalised",
            "ep18-report-links",
            "ep19-events-finalised",
            "ep20-audit-finalised",
        ],
        "summary-failed": [
            *_COMMON,
            "ep05-summary-failed",
            *run_reads,
            "ep13-proof-pass",
            "ep17-summary-failed",
            "ep18-report-links",
            "ep19-events-finalised",
            "ep20-audit-finalised",
        ],
        "failed-extract": [
            *_COMMON,
            "ep05-failed-extract",
            "ep09-new-read-page-2",
            "ep19-events-failed-extract",
            "ep20-audit-failed-extract",
        ],
        "failed-proof": [
            *_COMMON,
            "ep05-failed-proof",
            *run_reads,
            "ep13-proof-fail",
            "ep17-not-ready",
            "ep10-reconcile-pass",
            "ep19-events-pre-review",
            "ep20-audit-pre-review",
        ],
        "new-run": [
            *_COMMON,
            "ep03-create-run",
            "ep06-uploads",
            "ep07-new-pages",
            "ep08-pass",
            "ep09-new-read-page-1",
            "ep09-new-read-page-2",
            "ep10-reconcile-pass",
            "ep05-pre-review",
            "ep19-events-pre-review",
        ],
        "extracting": [
            *_COMMON,
            "ep05-extracting",
            "ep09-new-read-page-2",
            "ep10-reconcile-pass",
            "ep19-events-pre-review",
        ],
        "duplicate-run": [
            *_COMMON,
            "ep03-create-run",
            "ep06-uploads",
            "ep07-duplicate-run",
            "ep05-created",
        ],
        "validation-fail": [
            *_COMMON,
            "ep03-create-run",
            "ep06-uploads",
            "ep07-new-pages",
            "ep08-missing-worksheet",
            "ep05-validation-fail",
        ],
        "empty": ["ep01-health-ok", "ep02-me-controller", "ep04-runs-empty"],
    }


# --- output -----------------------------------------------------------------------------


class _IdMap:
    """Real UUID -> stable fake UUID, numbered in order of first appearance."""

    def __init__(self) -> None:
        self._ids: dict[str, str] = {}

    def text(self, value: str) -> str:
        return _UUID.sub(lambda m: self._one(m.group(0)), value)

    def _one(self, real: str) -> str:
        if real not in self._ids:
            self._ids[real] = FAKE_UUID.format(n=len(self._ids) + 1)
        return self._ids[real]

    def tree(self, value: Any) -> Any:
        """Rewrites every string (and dict key) in `value`, keys walked in sorted order."""
        if isinstance(value, dict):
            return {self.text(k): self.tree(value[k]) for k in sorted(value)}
        if isinstance(value, list):
            return [self.tree(v) for v in value]
        if isinstance(value, str):
            return self.text(value)
        return value


def _dump(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def render(rec: Recording, *, with_index: bool = True) -> dict[str, str]:
    """File name -> content: one file per fixture plus `index.json`."""
    ids = _IdMap()
    files: dict[str, str] = {}
    index: dict[str, Any] = {"fixtures": {}}
    for fx in rec.fixtures:
        body = ids.tree(fx.body)
        files[f"{fx.name}.json"] = _dump(body)
        index["fixtures"][fx.name] = {
            "endpoint": fx.endpoint,
            "method": fx.method,
            "template": fx.template,
            "path": ids.text(fx.path),
            "query": ids.tree(fx.query),
            "status": fx.status,
            "role": fx.role,
            "file": f"{fx.name}.json",
        }
    if not with_index:
        return files
    index["runs"] = {label: ids.text(i) for label, i in rec.runs.items()}
    index["review_items"] = {key: ids.text(i) for key, i in rec.review_items.items()}
    index["source_rows"] = dict(rec.source_rows)
    scenarios = _scenarios()
    for scenario, names in scenarios.items():
        missing = [n for n in names if n not in index["fixtures"]]
        if missing:
            raise AssertionError(f"scenario {scenario} names unknown fixtures: {missing}")
    index["scenarios"] = scenarios
    index["generated_by"] = "apps/api/scripts/record_web_fixtures.py"
    files["index.json"] = _dump(index)
    return files


def record(stop_after: str | None = None) -> dict[str, str]:
    """Runs both worlds and returns the rendered files (nothing written). With `stop_after`,
    stops once that fixture is recorded and returns only the fixtures so far, without
    `index.json`: a prefix of the full recording, so each file equals the full run's (a fast
    drift check)."""
    scenario = ScenarioClock(SCENARIO_START)
    tokens = Tokens()
    rec = Recording(stop_after=stop_after)
    logging.disable(logging.CRITICAL)
    try:
        with recording_process(scenario):
            record_main(World(rec, scenario, tokens))
            record_summary(World(rec, scenario, tokens))
    except _Stop:
        files = render(rec, with_index=False)
    else:
        files = render(rec)
    finally:
        logging.disable(logging.NOTSET)
    return files


def write(files: dict[str, str], out_dir: Path = OUTPUT_DIR) -> list[Path]:
    """Writes `files` into `out_dir`, removing stale `*.json` there first."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("*.json"):
        if stale.name not in files:
            stale.unlink()
    paths = []
    for name, content in sorted(files.items()):
        path = out_dir / name
        path.write_text(content, encoding="utf-8", newline="\n")
        paths.append(path)
    return paths


def main(argv: list[str] | None = None, out: Callable[[str], None] = print) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--out", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args(argv)
    paths = write(record(), args.out)
    out(f"wrote {len(paths)} files to {args.out}")


if __name__ == "__main__":
    main()
