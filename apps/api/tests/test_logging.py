"""Structured JSON request logs with redaction (F-11.R3, FR-030, research R3)."""

import json
import logging
from datetime import datetime
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.errors import ApiError
from app.domain.enums import ErrorCode
from app.main import create_app

REQUEST_FIELDS = {"at", "run_id", "endpoint", "method", "status", "duration_ms", "error_code"}


def _fake(prefix: str) -> str:
    """A distinctive fake secret built at runtime (no secret-looking literal in the source)."""
    return prefix + "-" + "Zq7" * 8


@pytest.fixture
def secrets(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    values = {
        "SUPABASE_SERVICE_ROLE_KEY": _fake("srk"),
        "OPENAI_API_KEY": _fake("oai"),
        "TELEGRAM_BOT_TOKEN": _fake("tgb"),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    return values


def _app() -> FastAPI:
    app = create_app()

    @app.get("/api/v1/runs/{run_id}/ping")
    def ping(run_id: str) -> dict[str, str]:
        return {"run": run_id}

    @app.get("/t/bad")
    def bad() -> None:
        raise ApiError(ErrorCode.BAD_MONTH, 400, "Month is not valid")

    @app.get("/t/boom")
    def boom() -> None:
        raise RuntimeError("boom")

    return app


def _lines(out: str) -> list[dict[str, Any]]:
    """Every stdout line is one JSON object."""
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _requests(out: str) -> list[dict[str, Any]]:
    return [line for line in _lines(out) if "endpoint" in line]


def test_F11_R3_one_json_line_per_request(
    frozen_clock: datetime, capsys: pytest.CaptureFixture[str]
) -> None:
    client = TestClient(_app(), raise_server_exceptions=False)
    capsys.readouterr()

    assert client.get("/api/v1/runs/run-42/ping").status_code == 200
    ok = _requests(capsys.readouterr().out)
    assert len(ok) == 1
    assert set(ok[0]) == REQUEST_FIELDS
    assert ok[0]["at"] == frozen_clock.isoformat()
    assert ok[0]["run_id"] == "run-42"
    assert ok[0]["endpoint"] == "/api/v1/runs/{run_id}/ping"
    assert ok[0]["method"] == "GET"
    assert ok[0]["status"] == 200
    assert ok[0]["error_code"] is None
    assert isinstance(ok[0]["duration_ms"], int) and ok[0]["duration_ms"] >= 0

    assert client.get("/t/bad").status_code == 400
    (bad,) = _requests(capsys.readouterr().out)
    assert (bad["status"], bad["error_code"], bad["run_id"]) == (400, "BAD_MONTH", None)

    assert client.get("/nowhere").status_code == 404
    (missing,) = _requests(capsys.readouterr().out)
    assert (missing["status"], missing["error_code"]) == (404, "NOT_FOUND")

    assert client.get("/t/boom").status_code == 500
    (boom,) = _requests(capsys.readouterr().out)
    assert (boom["status"], boom["error_code"]) == (500, "INTERNAL_ERROR")


def test_F11_R3_configure_logging_idempotent(capsys: pytest.CaptureFixture[str]) -> None:
    client = TestClient(_app())
    client = TestClient(_app())  # a second create_app() must not add a second handler
    capsys.readouterr()
    client.get("/api/v1/runs/r1/ping")
    assert len(_requests(capsys.readouterr().out)) == 1


def test_F11_R3_bearer_and_secret_values_redacted(
    secrets: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    srk, oai, tgb = secrets.values()
    bearer = "Bearer " + _fake("jwt")
    app = _app()

    @app.get("/t/leak")
    def leak() -> None:
        raise RuntimeError(f"upstream said {tgb} / {bearer}")

    client = TestClient(app, raise_server_exceptions=False)
    api_log = logging.getLogger("ledgerline.api")
    api_log.error("calling with %s and %s", srk, bearer)
    try:
        raise ValueError(f"bad key {oai}")
    except ValueError:
        api_log.exception("failed")
    assert client.get("/t/leak", headers={"Authorization": bearer}).status_code == 500

    out = capsys.readouterr().out
    assert _lines(out)  # still JSON lines, kept not dropped
    assert "[REDACTED]" in out and "Traceback" in out
    for value in (srk, oai, tgb, bearer.split()[1]):
        assert value not in out

    # uvicorn's own logger has its own handlers; the filter on the logger covers them too.
    seen: list[str] = []

    class _Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(self.format(record))

    uvicorn_log = logging.getLogger("uvicorn.error")
    handler = _Keep()
    uvicorn_log.addHandler(handler)
    try:
        try:
            raise OSError(f"socket {tgb}")
        except OSError:
            uvicorn_log.error("lost %s", srk, exc_info=True)
    finally:
        uvicorn_log.removeHandler(handler)
    assert seen and all(srk not in s and tgb not in s for s in seen)
    assert "[REDACTED]" in seen[0]


def test_F11_R3_no_request_or_file_bodies_logged(
    frozen_clock: datetime, capsys: pytest.CaptureFixture[str]
) -> None:
    markers = [_fake(m) for m in ("reqbody", "filebody", "respbody", "errmsg")]
    app = _app()

    @app.post("/api/v1/runs/{run_id}/echo")
    async def echo(run_id: str, request: Request) -> dict[str, str]:
        await request.body()
        return {"echo": markers[2]}

    @app.post("/t/reject")
    async def reject(request: Request) -> None:
        await request.body()
        raise ApiError(ErrorCode.BAD_REQUEST, 400, f"rejected {markers[3]}")

    client = TestClient(app)
    capsys.readouterr()
    client.post("/api/v1/runs/r9/echo", json={"note": markers[0]})
    client.post(
        "/api/v1/runs/r9/echo", files={"file": ("p1.jpg", markers[1].encode(), "image/jpeg")}
    )
    client.post("/t/reject", content=markers[1].encode())

    out = capsys.readouterr().out
    lines = _requests(out)
    assert [line["status"] for line in lines] == [200, 200, 400]
    assert lines[2]["error_code"] == "BAD_REQUEST"
    for marker in markers:
        assert marker not in out
