"""Safety of the integration test-project loader (tests/support/supabase_project.py), offline:
the contract suites must never write into the dev project of apps/api/.env."""

from pathlib import Path

import pytest

from tests.support.supabase_project import guard_test_project, load_test_project

_KEY = "-".join(["svc", "role", "placeholder"])  # built at runtime, not a real key


def _env(path: Path, **values: str) -> Path:
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    return path


def test_missing_env_test_skips(tmp_path: Path) -> None:
    with pytest.raises(pytest.skip.Exception):
        load_test_project(tmp_path / ".env.test", tmp_path / ".env")


def test_same_project_as_dev_fails(tmp_path: Path) -> None:
    url = "https://abcd.supabase.co"
    test = _env(
        tmp_path / ".env.test",
        SUPABASE_URL=url + "/",
        SUPABASE_SERVICE_ROLE_KEY=_KEY,
        SUPABASE_TEST_PROJECT_REF="abcd",
    )
    dev = _env(tmp_path / ".env", SUPABASE_URL=url)
    with pytest.raises(pytest.fail.Exception) as exc:
        load_test_project(test, dev)
    assert url not in str(exc.value)  # never printed


def test_ref_required_and_must_match_host(tmp_path: Path) -> None:
    dev = _env(tmp_path / ".env", SUPABASE_URL="https://devref.supabase.co")
    base = {"SUPABASE_URL": "https://testref.supabase.co", "SUPABASE_SERVICE_ROLE_KEY": _KEY}
    with pytest.raises(pytest.fail.Exception):
        load_test_project(_env(tmp_path / ".env.test", **base), dev)
    wrong = _env(tmp_path / ".env.test", **base, SUPABASE_TEST_PROJECT_REF="devref")
    with pytest.raises(pytest.fail.Exception):
        load_test_project(wrong, dev)
    ok = _env(tmp_path / ".env.test", **base, SUPABASE_TEST_PROJECT_REF="testref")
    project = load_test_project(ok, dev)
    assert project.ref == "testref"
    assert project.settings.SUPABASE_URL == "https://testref.supabase.co"


class _Rpc:
    def __init__(self, data: object) -> None:
        self.data = data

    def rpc(self, name: str, params: dict[str, object]) -> "_Rpc":
        return self

    def execute(self) -> "_Rpc":
        return self


def test_guard_compares_the_marked_ref() -> None:
    guard_test_project(_Rpc("testref"), "testref")
    with pytest.raises(pytest.fail.Exception):
        guard_test_project(_Rpc("devref"), "testref")


class _Recorder:
    """Answers `ledgerline_test_project` with a ref and records every rpc call."""

    def __init__(self, ref: str) -> None:
        self.ref = ref
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.data: object = None

    def rpc(self, name: str, params: dict[str, object]) -> "_Recorder":
        self.calls.append((name, params))
        self.data = self.ref if name == "ledgerline_test_project" else None
        return self

    def execute(self) -> "_Recorder":
        return self


def test_reset_passes_the_ref_to_the_database() -> None:
    from tests.support.supabase_project import reset_test_project

    client = _Recorder("testref")
    reset_test_project(client, "testref")
    assert client.calls[-1] == ("ledgerline_test_reset", {"p_ref": "testref"})
