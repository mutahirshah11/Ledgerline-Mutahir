"""Fault injection (Section 25, research R4): `?fault=` only counts when ALLOW_FAULTS=true."""

from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.errors import install_error_handlers
from app.core.faults import Fault, active_fault


def _client(monkeypatch: pytest.MonkeyPatch, allow: bool) -> TestClient:
    monkeypatch.setenv("ALLOW_FAULTS", "true" if allow else "false")
    get_settings.cache_clear()
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/probe")
    def probe(fault: Annotated[Fault | None, Depends(active_fault)]) -> dict[str, str | None]:
        return {"fault": None if fault is None else fault.value}

    return TestClient(app)


def test_fault_ignored_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, allow=False)
    assert client.get("/probe", params={"fault": "EXTRACT_TIMEOUT"}).json() == {"fault": None}
    assert client.get("/probe", params={"fault": "NOT_A_FAULT"}).json() == {"fault": None}


def test_fault_parsed_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, allow=True)
    for fault in Fault:
        assert client.get("/probe", params={"fault": fault.value}).json() == {"fault": fault.value}
    assert client.get("/probe").json() == {"fault": None}


def test_unknown_fault_400_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, allow=True)
    response = client.get("/probe", params={"fault": "NOT_A_FAULT"})
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "BAD_REQUEST"
    assert error["details"] == {"fault": "NOT_A_FAULT", "allowed": [f.value for f in Fault]}
