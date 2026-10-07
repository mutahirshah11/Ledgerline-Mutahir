"""006 T007: the web screen-test fixtures (`apps/web/e2e/fixtures/pack/`) are what the API
returns today (research R13). Regenerate with `uv run python -m scripts.record_web_fixtures`.

The full recording takes ~5 s (eight reconciles), so this drift check records only its prefix
up to the pre-review overview (~1 s): the upload, validate, extract (cached, new read and
upstream failure), reconcile and EP-05 fixtures, whose bytes must equal the committed ones.
"""

from pathlib import Path

from scripts.record_web_fixtures import OUTPUT_DIR, record, write

STOP_AFTER = "ep05-pre-review"


def test_committed_web_fixtures_match_the_api(tmp_path: Path) -> None:
    write(record(stop_after=STOP_AFTER), tmp_path)
    fresh = {p.name: p.read_text(encoding="utf-8") for p in tmp_path.glob("*.json")}
    assert f"{STOP_AFTER}.json" in fresh and "index.json" not in fresh
    committed = {name: OUTPUT_DIR / name for name in fresh}
    missing = sorted(name for name, path in committed.items() if not path.is_file())
    assert missing == [], "regenerate: uv run python -m scripts.record_web_fixtures"
    drifted = sorted(
        name for name, path in committed.items() if path.read_text(encoding="utf-8") != fresh[name]
    )
    assert drifted == [], f"regenerate: uv run python -m scripts.record_web_fixtures {drifted}"
