"""F-03 scripts: extraction eval (T028/T030, research R11, FR-032, FR-033) and seed cache
(T028/T031, contracts/extract-service.md §5-§6, research R10, FR-030, FR-031).

All tests are offline. Eval tests are synthetic: tiny ground-truth pages and reads built in
code, no pack images. Seed-cache tests capture the pack images with a `ScriptedModelClient`
answering the ground truth and write only under `tmp_path`. No model calls.
"""

import json
from datetime import datetime
from decimal import Decimal
from fractions import Fraction
from hashlib import sha256
from pathlib import Path

import openai
import pytest

import scripts.eval_extraction as eval_script
import scripts.seed_cache as seed_cache
from app.core.config import Settings
from app.core.errors import InvalidReadOutput
from app.core.memory_repo import MemoryRepo
from app.domain.enums import CellStatus, ReadVariant, RegisterField
from app.domain.models import Cell, Extraction, GroundTruthPage, PageRead, RowRead
from app.pipeline.extract import cache_file_name, load_cached_extractions
from app.pipeline.model_client import CachedReadAmbiguous, RawRead
from scripts.eval_extraction import (
    CacheEmpty,
    ReadPair,
    cost_usd,
    evaluate,
    exit_code,
    gate_passed,
    load_cached_reads,
    load_ground_truth,
    main,
    parse_pages,
)
from tests.fakes.model_clients import ScriptedModelClient
from tests.support.extracted import ground_truth_read, page_image
from tests.support.pack import PAGES

SHA = "a" * 64

# =========================================================================================
# Eval (scripts/eval_extraction.py) — T028 eval tests
# =========================================================================================


def _clear(value: str | None) -> Cell:
    return Cell(value=value, status=CellStatus.CLEAR)


def _row(index: int, **overrides: Cell) -> RowRead:
    """A row whose 8 cells are CLEAR "<field>-<index>" unless overridden."""
    cells = {f: overrides.get(f.value, _clear(f"{f.value}-{index}")) for f in RegisterField}
    return RowRead(row_index=index, cells=cells)


def _read(*rows: RowRead) -> PageRead:
    return PageRead(
        month_written="Sep 2026",
        page_no_written="1",
        table_top=0.1,
        table_bottom=0.9,
        blank_row_slots=0,
        rows=list(rows),
    )


def _gt(page_no: int, *rows: RowRead) -> GroundTruthPage:
    read = _read(*rows)
    return GroundTruthPage(**read.model_dump(), page_no=page_no, source="ground_truth")


def _pair(page_no: int, read_a: PageRead, read_b: PageRead | None = None) -> ReadPair:
    return ReadPair(
        page_no=page_no,
        page_sha256=SHA,
        read_a=read_a,
        read_b=read_a if read_b is None else read_b,
        input_tokens=0,
        output_tokens=0,
    )


def test_F03_eval_accuracy_formula() -> None:
    # GT: 2 rows = 16 CLEAR cells. Read: one wrong value, one UNCLEAR, one matching only after
    # trim + case-fold, one CLEAR blank that GT also has blank.
    gt = _gt(1, _row(1, unit=_clear(None)), _row(2))
    read = _read(
        _row(1, unit=_clear("  "), supplier=_clear("  SUPPLIER-1 ")),
        _row(
            2,
            qty=_clear("999"),
            lot_no=Cell(value=None, status=CellStatus.UNCLEAR, candidates=["lot_no-2"]),
        ),
    )
    report = evaluate({1: gt}, [_pair(1, read)])
    # accuracy = GT CLEAR cells read CLEAR with the "same" value / all GT CLEAR cells
    assert (report.merged.correct, report.merged.total) == (14, 16)
    assert report.merged.accuracy == Fraction(14, 16)
    assert {(m.row_index, m.field) for m in report.mismatches} == {(2, "qty"), (2, "lot_no")}
    # per-read accuracy uses the same rule on each read alone
    assert (report.read_a.correct, report.read_a.total) == (14, 16)
    # a GT row missing from both reads counts every CLEAR cell as wrong
    missing = evaluate({1: gt}, [_pair(1, _read(_row(1, unit=_clear(None))))])
    assert (missing.merged.correct, missing.merged.total) == (8, 16)


def test_F03_eval_accuracy_counts_reads_disagreement_as_wrong() -> None:
    gt = _gt(1, _row(1))
    report = evaluate({1: gt}, [_pair(1, _read(_row(1)), _read(_row(1, qty=_clear("7"))))])
    assert (report.merged.correct, report.merged.total) == (7, 8)
    assert (report.read_a.correct, report.read_b.correct) == (8, 7)
    assert [(d.row_index, d.field) for d in report.disagreements] == [(1, "qty")]


def test_F03_eval_guessed_count() -> None:
    illegible = Cell(value=None, status=CellStatus.ILLEGIBLE)
    gt = _gt(2, _row(14, qty=illegible))
    guessed = evaluate({2: gt}, [_pair(2, _read(_row(14, qty=_clear("40"))))])
    assert [(g.page_no, g.row_index, g.field) for g in guessed.guessed] == [(2, 14, "qty")]
    # ILLEGIBLE cells are not in the accuracy denominator
    assert guessed.merged.total == 7
    honest = evaluate({2: gt}, [_pair(2, _read(_row(14, qty=illegible)))])
    assert honest.guessed == []
    blank = evaluate({2: gt}, [_pair(2, _read(_row(14, qty=_clear(None))))])
    assert blank.guessed == []  # CLEAR with no value is not a guess (R11: non-null value)
    # one read guessing is caught by the merge (UNCLEAR), not counted as guessed
    one_read = _read(_row(14, qty=_clear("40")))
    split = evaluate({2: gt}, [_pair(2, _read(_row(14, qty=illegible)), one_read)])
    assert split.guessed == []


def test_F03_eval_corrected_scoring() -> None:
    corrected = Cell(value="66", status=CellStatus.CORRECTED, struck="60")
    gt = _gt(2, _row(5, qty=corrected))
    good = evaluate({2: gt}, [_pair(2, _read(_row(5, qty=corrected)))])
    assert (good.corrected.correct, good.corrected.total) == (1, 1)
    assert good.merged.total == 7  # CORRECTED cells are scored apart from the CLEAR accuracy
    assert good.mismatches == []
    for wrong in (
        _clear("66"),
        Cell(value="66", status=CellStatus.CORRECTED, struck="80"),
        Cell(value="68", status=CellStatus.CORRECTED, struck="60"),
    ):
        bad = evaluate({2: gt}, [_pair(2, _read(_row(5, qty=wrong)))])
        assert (bad.corrected.correct, bad.corrected.total) == (0, 1)
        assert [(m.row_index, m.field, m.expected_status) for m in bad.mismatches] == [
            (5, "qty", "CORRECTED")
        ]


def test_F03_eval_exit_codes() -> None:
    # the gate compares exact fractions: correct / total >= 98 / 100 and guessed == 0
    assert gate_passed(50, 50, guessed=0)
    assert gate_passed(49, 50, guessed=0)  # exactly 0.98 passes
    assert not gate_passed(391, 400, guessed=0)  # 0.9775
    assert not gate_passed(50, 50, guessed=1)
    assert not gate_passed(0, 0, guessed=0)  # nothing scored is not a pass

    illegible = Cell(value=None, status=CellStatus.ILLEGIBLE)
    gt = {1: _gt(1, *(_row(i) for i in range(1, 8)), _row(8, qty=illegible))}  # 63 CLEAR
    perfect = [_row(i) for i in range(1, 8)] + [_row(8, qty=illegible)]
    assert exit_code(evaluate(gt, [_pair(1, _read(*perfect))])) == 0
    one_wrong = [_row(1, qty=_clear("0"))] + perfect[1:]  # 62/63 = 0.984
    assert exit_code(evaluate(gt, [_pair(1, _read(*one_wrong))])) == 0
    two_wrong = [_row(1, qty=_clear("0"), unit=_clear("0"))] + perfect[1:]  # 61/63 = 0.968
    assert exit_code(evaluate(gt, [_pair(1, _read(*two_wrong))])) == 1
    guess = perfect[:7] + [_row(8, qty=_clear("5"))]
    assert exit_code(evaluate(gt, [_pair(1, _read(*guess))])) == 1


def test_F03_eval_cli_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    gt = {1: _gt(1, _row(1)), 2: _gt(2, _row(1))}

    def good(pages: tuple[int, ...], live: bool) -> list[ReadPair]:
        return [_pair(p, _read(_row(1))) for p in pages]

    def bad(pages: tuple[int, ...], live: bool) -> list[ReadPair]:
        return [_pair(p, _read(_row(1, qty=_clear("0")))) for p in pages]

    def empty(pages: tuple[int, ...], live: bool) -> list[ReadPair]:
        raise CacheEmpty("no cached reads in supabase/seed/extractions")

    assert main([], loader=good, ground_truth=gt) == 0
    assert main([], loader=bad, ground_truth=gt) == 1
    capsys.readouterr()
    assert main([], loader=empty, ground_truth=gt) == 2
    assert "seed_cache --capture" in capsys.readouterr().err


def test_F03_eval_cost_formula() -> None:
    # cost = (input tokens x price_in + output tokens x price_out) / 1e6, exact Decimal
    assert cost_usd(1_000_000, 500_000, Decimal("1.25"), Decimal("10")) == Decimal("6.25")
    assert cost_usd(12_345, 678, Decimal("2"), Decimal("8")) == Decimal("0.030114")
    assert cost_usd(10, 10, None, Decimal("1")) is None  # no price, no cost
    gt = {1: _gt(1, _row(1))}
    pair = ReadPair(1, SHA, _read(_row(1)), _read(_row(1)), 3000, 1000)
    report = evaluate(gt, [pair], price_in=Decimal("1"), price_out=Decimal("4"))
    assert (report.input_tokens, report.output_tokens) == (3000, 1000)
    assert report.cost_usd == Decimal("0.007")


def test_F03_eval_pages_option(capsys: pytest.CaptureFixture[str]) -> None:
    assert parse_pages("1,2") == (1, 2)
    assert parse_pages(" 2 ") == (2,)
    assert parse_pages("2,1,2") == (1, 2)
    for bad in ("3", "", "a", "0"):
        with pytest.raises(ValueError):
            parse_pages(bad)

    seen: list[tuple[tuple[int, ...], bool]] = []
    gt = {1: _gt(1, _row(1)), 2: _gt(2, _row(1))}

    def loader(pages: tuple[int, ...], live: bool) -> list[ReadPair]:
        seen.append((pages, live))
        return [_pair(p, _read(_row(1))) for p in pages]

    assert main(["--pages", "2", "--json"], loader=loader, ground_truth=gt) == 0
    assert seen == [((2,), False)]
    out = json.loads(capsys.readouterr().out)
    assert out["pages"] == [2]
    assert out["merged"]["total"] == 8
    assert out["passed"] is True
    # --live reaches the (injected) loader as live=True; no network here
    seen.clear()
    assert main(["--live", "--pages", "1", "--json"], loader=loader, ground_truth=gt) == 0
    assert seen == [((1,), True)]


def test_F03_eval_cli_load_errors_exit_2(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    gt = {1: _gt(1, _row(1)), 2: _gt(2, _row(1))}
    fake_key = "sk-" + "-".join(["proj", "not", "real"])  # built at runtime: no literal key
    errors: list[Exception] = [
        ValueError("cached read x.json: read_variant is not A"),
        OSError("no such file t4_register_page1.jpg"),
        KeyError("no cached read for abc"),
        CachedReadAmbiguous("ambiguous cached reads (a, b)"),
        InvalidReadOutput("rows: not a list"),
        openai.OpenAIError(f"Incorrect API key provided: {fake_key}"),
        RuntimeError("boom"),
    ]
    for error in errors:

        def loader(pages: tuple[int, ...], live: bool, error: Exception = error) -> list[ReadPair]:
            raise error

        assert main([], loader=loader, ground_truth=gt) == 2, type(error).__name__
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.startswith(f"eval: {type(error).__name__}: "), captured.err
        assert "Traceback" not in captured.err
        assert fake_key not in captured.err  # an API key is never echoed

    # --live without OPENAI_API_KEY (hermetic settings: none set) -> 2, through the real loader
    monkeypatch.setattr(eval_script, "get_settings", lambda: Settings())
    assert main(["--live", "--pages", "1"], ground_truth=gt) == 2
    err = capsys.readouterr().err
    assert err.startswith("eval: RuntimeError: ") and "OPENAI_API_KEY" in err


def test_F03_eval_report_lists_non_clear_cells_and_extra_rows(
    capsys: pytest.CaptureFixture[str],
) -> None:
    corrected = Cell(value="66", status=CellStatus.CORRECTED, struck="60")
    illegible = Cell(value=None, status=CellStatus.ILLEGIBLE)
    gt = {2: _gt(2, _row(5, qty=corrected), _row(14, qty=illegible))}
    unclear = Cell(value=None, status=CellStatus.UNCLEAR, candidates=["40", "46"])
    read_a = _read(_row(5, qty=corrected), _row(14, qty=unclear), _row(15), _row(16))
    read_b = _read(_row(5, qty=corrected), _row(14, qty=unclear), _row(15))
    report = evaluate(gt, [_pair(2, read_a, read_b)])
    # every GT non-CLEAR cell with the merged status and value
    got = [
        (c.page_no, c.row_index, c.field, c.expected_status, c.got_status, c.got_value)
        for c in report.non_clear
    ]
    assert got == [
        (2, 5, "qty", "CORRECTED", "CORRECTED", "66"),
        (2, 14, "qty", "ILLEGIBLE", "UNCLEAR", None),
    ]
    # informational: merged rows absent from GT, and rows found by one read only
    assert [(r.page_no, r.row_index) for r in report.extra_rows] == [(2, 15), (2, 16)]
    assert report.row_in_one_read == 1
    assert exit_code(report) == 0  # neither changes the exit code
    out = report.as_json()
    assert out["non_clear_cells"][1] == {
        "page_no": 2,
        "row_index": 14,
        "field": "qty",
        "expected": {"status": "ILLEGIBLE", "value": None, "struck": None},
        "got": {"status": "UNCLEAR", "value": None, "struck": None},
    }
    assert out["extra_rows"] == [{"page_no": 2, "row_index": 15}, {"page_no": 2, "row_index": 16}]
    assert out["row_in_one_read"] == 1

    def loader(pages: tuple[int, ...], live: bool) -> list[ReadPair]:
        return [_pair(2, read_a, read_b)]

    assert main([], loader=loader, ground_truth=gt) == 0
    text = capsys.readouterr().out
    assert "non-CLEAR p2 r14 qty: expected ILLEGIBLE; got UNCLEAR None" in text
    assert "non-CLEAR p2 r05 qty: expected CORRECTED; got CORRECTED '66' struck '60'" in text
    assert "extra rows       2" in text
    assert "row in one read  1" in text


def test_F03_eval_projected_cost_for_all_pack_pages(capsys: pytest.CaptureFixture[str]) -> None:
    gt = {1: _gt(1, _row(1)), 2: _gt(2, _row(1))}

    def loader(pages: tuple[int, ...], live: bool) -> list[ReadPair]:
        return [ReadPair(p, SHA, _read(_row(1)), _read(_row(1)), 3000, 1000) for p in pages]

    prices = ["--price-in", "1", "--price-out", "4"]
    assert main(["--pages", "1", *prices, "--json"], loader=loader, ground_truth=gt) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["cost_usd"] == "0.007"
    assert Decimal(out["projected_cost_usd"]) == Decimal("0.014")  # scaled per page to 2 pages
    assert main(["--pages", "1", *prices], loader=loader, ground_truth=gt) == 0
    assert "projected cost   US$0.014000 for all 2 pack pages" in capsys.readouterr().out
    # all pages, or no prices: no projection
    assert main([*prices, "--json"], loader=loader, ground_truth=gt) == 0
    assert json.loads(capsys.readouterr().out)["projected_cost_usd"] is None
    assert main(["--pages", "1", "--json"], loader=loader, ground_truth=gt) == 0
    assert json.loads(capsys.readouterr().out)["projected_cost_usd"] is None


def test_F03_eval_load_cached_reads_empty_dir(tmp_path: Path) -> None:
    with pytest.raises(CacheEmpty):
        load_cached_reads((1, 2), cache_dir=tmp_path, model="m", prompt_version="v1")


def _write_replay_cache(
    cache_dir: Path, pack: Path, page_no: int, read: PageRead, tokens: tuple[int, int]
) -> str:
    """One page's image plus A/B replay files in the format `ReplayModelClient` reads."""
    image = f"page {page_no} image".encode()
    (pack / f"t4_register_page{page_no}.jpg").write_bytes(image)
    sha = sha256(image).hexdigest()
    for variant in ReadVariant:
        record = {
            "page_sha256": sha,
            "read_variant": variant.value,
            "result": read.model_dump(mode="json"),
            "input_tokens": tokens[0],
            "output_tokens": tokens[1],
            "latency_ms": 5,
        }
        name = f"{sha}.{variant.value}.m.v1.json"
        (cache_dir / name).write_text(json.dumps(record), encoding="utf-8")
    return sha


def test_F03_eval_load_cached_reads_from_replay_files(tmp_path: Path) -> None:
    cache_dir, pack = tmp_path / "cache", tmp_path / "pack"
    cache_dir.mkdir()
    pack.mkdir()
    read_1, read_2 = _read(_row(1)), _read(_row(1), _row(2))
    sha_1 = _write_replay_cache(cache_dir, pack, 1, read_1, (100, 10))
    sha_2 = _write_replay_cache(cache_dir, pack, 2, read_2, (200, 20))
    pairs = load_cached_reads(
        (1, 2), cache_dir=cache_dir, pack_dir=pack, model="m", prompt_version="v1"
    )
    assert [(p.page_no, p.page_sha256) for p in pairs] == [(1, sha_1), (2, sha_2)]
    assert (pairs[0].read_a, pairs[0].read_b, pairs[1].read_a) == (read_1, read_1, read_2)
    assert [(p.input_tokens, p.output_tokens) for p in pairs] == [(200, 20), (400, 40)]


def test_F03_eval_load_cached_reads_partial_cache(tmp_path: Path) -> None:
    cache_dir, pack = tmp_path / "cache", tmp_path / "pack"
    cache_dir.mkdir()
    pack.mkdir()
    _write_replay_cache(cache_dir, pack, 1, _read(_row(1)), (1, 1))
    (pack / "t4_register_page2.jpg").write_bytes(b"page 2 image")  # page 2 never captured
    with pytest.raises(CacheEmpty):
        load_cached_reads(
            (1, 2), cache_dir=cache_dir, pack_dir=pack, model="m", prompt_version="v1"
        )


def test_F03_eval_ground_truth_scores_itself_perfectly() -> None:
    gt = load_ground_truth()
    pairs = [_pair(p, gt[p]) for p in (1, 2)]
    report = evaluate(gt, pairs)
    assert (report.merged.correct, report.merged.total) == (222, 222)
    assert (report.corrected.correct, report.corrected.total) == (1, 1)
    assert report.guessed == [] and report.mismatches == [] and report.disagreements == []
    assert exit_code(report) == 0


# =========================================================================================
# Seed cache (scripts/seed_cache.py) — T028 seed tests, T031
# =========================================================================================

SEED_MODEL = "test-model"
SEED_PROMPT = "v1"


def _seed_settings() -> Settings:
    return Settings(_env_file=None, EXTRACT_MODEL=SEED_MODEL, PROMPT_VERSION=SEED_PROMPT)


def _seed_client(ground_truth_dir: Path) -> ScriptedModelClient:
    """Answers pages 1 then 2 for each variant with the ground truth (pages read in order)."""
    answers = [
        RawRead(
            payload=ground_truth_read(ground_truth_dir, p).model_dump(mode="json"),
            input_tokens=1000 + p,
            output_tokens=500 + p,
            latency_ms=10,
        )
        for p in PAGES
    ]
    return ScriptedModelClient({v: list(answers) for v in ReadVariant})


def _seed_names(pack_dir: Path) -> set[str]:
    return {
        cache_file_name(sha256(page_image(pack_dir, p)).hexdigest(), v, SEED_MODEL, SEED_PROMPT)
        for p in PAGES
        for v in ReadVariant
    }


def _seed_capture(tmp_path: Path, pack_dir: Path, ground_truth_dir: Path) -> list[Path]:
    return seed_cache.capture(
        client=_seed_client(ground_truth_dir),
        settings=_seed_settings(),
        cache_dir=tmp_path,
        pack_dir=pack_dir,
    )


def test_F03_seed_cache_file_name_format(
    tmp_path: Path, pack_dir: Path, ground_truth_dir: Path, frozen_clock: datetime
) -> None:
    """Capture writes 4 files `{sha}.{variant}.{model}.{prompt_version}.json`, each a full
    validated `Extraction` whose key matches its name (research R10)."""
    sha = "ab" * 32
    assert cache_file_name(sha, ReadVariant.B, "m-1", "v2") == f"{sha}.B.m-1.v2.json"

    written = _seed_capture(tmp_path, pack_dir, ground_truth_dir)

    assert {p.name for p in written} == _seed_names(pack_dir)
    assert {p.name for p in tmp_path.iterdir()} == _seed_names(pack_dir)
    for path in written:
        record = Extraction.model_validate_json(path.read_text(encoding="utf-8"))
        assert path.name == cache_file_name(
            record.page_sha256, record.read_variant, record.model, record.prompt_version
        )
        assert record.input_tokens > 0 and record.output_tokens > 0
        assert record.created_at == frozen_clock


def test_F03_seed_cache_capture_stores_the_validated_reads(
    tmp_path: Path, pack_dir: Path, ground_truth_dir: Path, frozen_clock: datetime
) -> None:
    """Each file's `result` is the read the model returned (validated), page by page."""
    _seed_capture(tmp_path, pack_dir, ground_truth_dir)
    for p in PAGES:
        sha = sha256(page_image(pack_dir, p)).hexdigest()
        for v in ReadVariant:
            path = tmp_path / cache_file_name(sha, v, SEED_MODEL, SEED_PROMPT)
            record = Extraction.model_validate_json(path.read_text(encoding="utf-8"))
            assert record.result == ground_truth_read(ground_truth_dir, p)


def test_F03_seed_cache_refuses_overwrite(
    tmp_path: Path, pack_dir: Path, ground_truth_dir: Path, frozen_clock: datetime
) -> None:
    """An existing file for any of the 4 keys: capture refuses before any model call and
    leaves the directory unchanged (Clarification 4: the cache is never rewritten)."""
    existing = tmp_path / sorted(_seed_names(pack_dir))[0]
    existing.write_text("keep me", encoding="utf-8")
    client = _seed_client(ground_truth_dir)

    with pytest.raises(FileExistsError, match=existing.name):
        seed_cache.capture(
            client=client, settings=_seed_settings(), cache_dir=tmp_path, pack_dir=pack_dir
        )

    assert client.calls == 0
    assert existing.read_text(encoding="utf-8") == "keep me"
    assert [p.name for p in tmp_path.iterdir()] == [existing.name]


def test_F03_seed_cache_capture_without_key_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No OPENAI_API_KEY (hermetic settings): `--capture` exits 2 with one clear line, writes
    nothing and never builds a client."""
    code = seed_cache.main(["--capture"], cache_dir=tmp_path)

    assert code == 2
    err = capsys.readouterr().err
    assert "OPENAI_API_KEY" in err
    assert "Traceback" not in err
    assert list(tmp_path.iterdir()) == []


def test_F03_seed_cache_capture_cli_uses_injected_client(
    tmp_path: Path,
    pack_dir: Path,
    ground_truth_dir: Path,
    frozen_clock: datetime,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--capture` with a client factory (tests) writes the 4 files and exits 0."""
    code = seed_cache.main(
        ["--capture"],
        cache_dir=tmp_path,
        pack_dir=pack_dir,
        settings=_seed_settings(),
        client_factory=lambda _settings: _seed_client(ground_truth_dir),
    )
    assert code == 0
    assert {p.name for p in tmp_path.iterdir()} == _seed_names(pack_dir)
    assert "wrote 4" in capsys.readouterr().out


def test_F03_seed_cache_capture_cli_refuses_overwrite_exit_2(
    tmp_path: Path,
    pack_dir: Path,
    ground_truth_dir: Path,
    frozen_clock: datetime,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_capture(tmp_path, pack_dir, ground_truth_dir)
    client = _seed_client(ground_truth_dir)
    code = seed_cache.main(
        ["--capture"],
        cache_dir=tmp_path,
        pack_dir=pack_dir,
        settings=_seed_settings(),
        client_factory=lambda _settings: client,
    )
    assert code == 2
    assert "refusing to overwrite" in capsys.readouterr().err
    assert client.calls == 0


def test_F03_load_cached_extractions_idempotent(
    tmp_path: Path, pack_dir: Path, ground_truth_dir: Path, frozen_clock: datetime
) -> None:
    """Loading the 4 captured files stores 4; a second load stores nothing new (FR-031)."""
    _seed_capture(tmp_path, pack_dir, ground_truth_dir)
    repo = MemoryRepo()

    assert load_cached_extractions(repo, tmp_path) == 4
    assert load_cached_extractions(repo, tmp_path) == 0
    for p in PAGES:
        sha = sha256(page_image(pack_dir, p)).hexdigest()
        for v in ReadVariant:
            assert repo.get_extraction(sha, v, SEED_MODEL, SEED_PROMPT) is not None


def test_F03_seed_cache_load_cli(
    tmp_path: Path,
    pack_dir: Path,
    ground_truth_dir: Path,
    frozen_clock: datetime,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--load` loads into the configured repo (MemoryRepo until Phase 3) and reports the
    count; loading again into the same repo stores 0."""
    _seed_capture(tmp_path, pack_dir, ground_truth_dir)
    repo = MemoryRepo()

    assert seed_cache.main(["--load"], cache_dir=tmp_path, repo_factory=lambda: repo) == 0
    assert "loaded 4" in capsys.readouterr().out
    assert seed_cache.main(["--load"], cache_dir=tmp_path, repo_factory=lambda: repo) == 0
    assert "loaded 0" in capsys.readouterr().out


def test_F03_seed_cache_load_cli_empty_cache(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only `.gitkeep` (nothing captured yet): `--load` stores 0, says so, exits 0."""
    (tmp_path / ".gitkeep").write_text("")
    assert seed_cache.main(["--load"], cache_dir=tmp_path) == 0
    out = capsys.readouterr().out
    assert "loaded 0" in out
    assert "--capture" in out


def test_F03_seed_cache_requires_one_mode(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        seed_cache.main([])
    assert exc.value.code == 2


def test_F03_seed_cache_capture_error_masks_key(
    tmp_path: Path, pack_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed read: exit 2, one line, anything shaped like an API key masked, nothing written."""
    client = ScriptedModelClient({v: [ValueError("bad key sk-proj-ABC_123")] for v in ReadVariant})
    code = seed_cache.main(
        ["--capture"],
        cache_dir=tmp_path,
        pack_dir=pack_dir,
        settings=_seed_settings(),
        client_factory=lambda _settings: client,
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "sk-proj-ABC_123" not in err
    assert "sk-***" in err
    assert len(err.strip().splitlines()) == 1
    assert list(tmp_path.iterdir()) == []
