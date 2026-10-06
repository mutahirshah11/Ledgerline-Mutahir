"""Extraction eval: score merged register reads against the ground truth (F-03.AC6, research R11).

Usage (from apps/api)::

    uv run python -m scripts.eval_extraction                      # committed cache (offline)
    uv run python -m scripts.eval_extraction --live --pages 1 --price-in 1.25 --price-out 10
    uv run python -m scripts.eval_extraction --json               # JSON report on stdout

Scoring, per ground-truth (GT) cell, using the merge's "same" rule (trim + case-fold):

- GT `CLEAR`: correct when the merged cell is `CLEAR` with the same value.
  accuracy = correct GT-`CLEAR` cells / all GT-`CLEAR` cells, kept as an exact `Fraction`.
- GT `ILLEGIBLE`: guessed when the merged cell is `CLEAR` with a non-null value.
- GT `CORRECTED`: correct when the merged cell is `CORRECTED` with the same struck and value
  (reported apart; not part of the accuracy denominator).

Per-read accuracy applies the same rule to read A and read B alone. Cost (US$) =
(input tokens x price_in + output tokens x price_out) / 1e6, prices in US$ per million tokens
from the command line (never hard-coded). Exit codes: 0 when accuracy >= 0.98 and guessed == 0;
1 otherwise; 2 when reads cannot be loaded (empty cache, missing key, bad arguments).

Offline mode reads the committed cache (`supabase/seed/extractions/`) through
`ReplayModelClient`; `--live` reads afresh with `OpenAIModelClient` and writes nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from hashlib import sha256
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.domain.enums import REVIEWABLE_REGISTER_FIELDS, CellStatus, ReadVariant, ReviewReason
from app.domain.models import Cell, GroundTruthPage, MergedPage, PageRead
from app.pipeline.extract import _same_key, load_prompt, load_read_schema, merge_reads, parse_read
from app.pipeline.model_client import ModelClient, OpenAIModelClient, ReplayModelClient

SCRIPTS_DIR = Path(__file__).resolve().parent
API_DIR = SCRIPTS_DIR.parent
REPO_ROOT = API_DIR.parent.parent
CACHE_DIR = REPO_ROOT / "supabase" / "seed" / "extractions"
PACK_DIR = REPO_ROOT / "data" / "kestrel-2026-09"
GROUND_TRUTH_DIR = API_DIR / "tests" / "ground_truth"
PACK_PAGES = (1, 2)
ACCURACY_GATE = Fraction(98, 100)
EXIT_PASS, EXIT_FAIL, EXIT_ERROR = 0, 1, 2


class CacheEmpty(RuntimeError):
    """The committed cache has no read for a requested page (nothing captured yet)."""


@dataclass(frozen=True, slots=True)
class ReadPair:
    """Both reads of one page, with tokens summed over the two reads."""

    page_no: int
    page_sha256: str
    read_a: PageRead
    read_b: PageRead
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class CellRef:
    page_no: int
    row_index: int
    field: str


@dataclass(frozen=True, slots=True)
class Mismatch:
    """A GT `CLEAR` or `CORRECTED` cell the merged read did not get right."""

    page_no: int
    row_index: int
    field: str
    expected_status: str
    expected_value: str | None
    expected_struck: str | None
    got_status: str | None
    got_value: str | None
    got_struck: str | None


@dataclass(slots=True)
class Score:
    correct: int = 0
    total: int = 0

    @property
    def accuracy(self) -> Fraction | None:
        return Fraction(self.correct, self.total) if self.total else None

    def as_json(self) -> dict[str, Any]:
        acc = self.accuracy
        return {
            "correct": self.correct,
            "total": self.total,
            "accuracy": None if acc is None else round(float(acc), 6),
        }


@dataclass(slots=True)
class EvalReport:
    pages: list[int]
    merged: Score = field(default_factory=Score)
    read_a: Score = field(default_factory=Score)
    read_b: Score = field(default_factory=Score)
    corrected: Score = field(default_factory=Score)
    guessed: list[CellRef] = field(default_factory=list)
    mismatches: list[Mismatch] = field(default_factory=list)
    disagreements: list[CellRef] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal | None = None

    @property
    def passed(self) -> bool:
        return gate_passed(self.merged.correct, self.merged.total, guessed=len(self.guessed))

    def as_json(self) -> dict[str, Any]:
        return {
            "pages": self.pages,
            "passed": self.passed,
            "merged": self.merged.as_json(),
            "read_a": self.read_a.as_json(),
            "read_b": self.read_b.as_json(),
            "corrected": self.corrected.as_json(),
            "guessed": [_ref_json(g) for g in self.guessed],
            "mismatches": [
                {
                    "page_no": m.page_no,
                    "row_index": m.row_index,
                    "field": m.field,
                    "expected": {
                        "status": m.expected_status,
                        "value": m.expected_value,
                        "struck": m.expected_struck,
                    },
                    "got": {"status": m.got_status, "value": m.got_value, "struck": m.got_struck},
                }
                for m in self.mismatches
            ],
            "reviewable_disagreements": [_ref_json(d) for d in self.disagreements],
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": None if self.cost_usd is None else str(self.cost_usd),
        }


def _ref_json(ref: CellRef) -> dict[str, Any]:
    return {"page_no": ref.page_no, "row_index": ref.row_index, "field": ref.field}


# --- pure scoring -------------------------------------------------------------------------


def gate_passed(correct: int, total: int, *, guessed: int) -> bool:
    """F-03.AC6 / FR-033: correct / total >= 0.98 (exact) and nothing guessed."""
    return total > 0 and Fraction(correct, total) >= ACCURACY_GATE and guessed == 0


def exit_code(report: EvalReport) -> int:
    return EXIT_PASS if report.passed else EXIT_FAIL


def cost_usd(
    input_tokens: int, output_tokens: int, price_in: Decimal | None, price_out: Decimal | None
) -> Decimal | None:
    """(input tokens x price_in + output tokens x price_out) / 1e6; None without both prices."""
    if price_in is None or price_out is None:
        return None
    return (input_tokens * price_in + output_tokens * price_out) / Decimal(1_000_000)


def _clear_correct(gt: Cell, got: Cell | None) -> bool:
    return (
        got is not None
        and got.status is CellStatus.CLEAR
        and _same_key(got.value) == _same_key(gt.value)
    )


def _corrected_correct(gt: Cell, got: Cell | None) -> bool:
    return (
        got is not None
        and got.status is CellStatus.CORRECTED
        and _same_key(got.value) == _same_key(gt.value)
        and _same_key(got.struck) == _same_key(gt.struck)
    )


def _cells(read: PageRead) -> dict[tuple[int, str], Cell]:
    return {(r.row_index, f.value): c for r in read.rows for f, c in r.cells.items()}


def _merged_cells(page: MergedPage) -> dict[tuple[int, str], Cell]:
    return {(r.row_index, f.value): c.merged for r in page.rows for f, c in r.cells.items()}


def _score_clear(gt: PageRead, got: Mapping[tuple[int, str], Cell], score: Score) -> None:
    for (row_index, name), cell in _cells(gt).items():
        if cell.status is CellStatus.CLEAR:
            score.total += 1
            score.correct += _clear_correct(cell, got.get((row_index, name)))


def evaluate(
    ground_truth: Mapping[int, PageRead],
    pairs: Sequence[ReadPair],
    *,
    price_in: Decimal | None = None,
    price_out: Decimal | None = None,
) -> EvalReport:
    """Merge each page's two reads (`merge_reads`) and score them against the ground truth."""
    report = EvalReport(pages=sorted(p.page_no for p in pairs))
    for pair in sorted(pairs, key=lambda p: p.page_no):
        gt = ground_truth[pair.page_no]
        page = merge_reads(
            pair.read_a, pair.read_b, page_no=pair.page_no, page_sha256=pair.page_sha256
        )
        merged = _merged_cells(page)
        _score_clear(gt, _cells(pair.read_a), report.read_a)
        _score_clear(gt, _cells(pair.read_b), report.read_b)
        for (row_index, name), cell in _cells(gt).items():
            got = merged.get((row_index, name))
            ok: bool
            if cell.status is CellStatus.CLEAR:
                report.merged.total += 1
                ok = _clear_correct(cell, got)
                report.merged.correct += ok
            elif cell.status is CellStatus.CORRECTED:
                report.corrected.total += 1
                ok = _corrected_correct(cell, got)
                report.corrected.correct += ok
            else:
                if (
                    cell.status is CellStatus.ILLEGIBLE
                    and got is not None
                    and got.status is CellStatus.CLEAR
                    and got.value is not None
                ):
                    report.guessed.append(CellRef(pair.page_no, row_index, name))
                continue
            if not ok:
                report.mismatches.append(
                    Mismatch(
                        page_no=pair.page_no,
                        row_index=row_index,
                        field=name,
                        expected_status=cell.status.value,
                        expected_value=cell.value,
                        expected_struck=cell.struck,
                        got_status=None if got is None else got.status.value,
                        got_value=None if got is None else got.value,
                        got_struck=None if got is None else got.struck,
                    )
                )
        for row in page.rows:
            for reg_field, mc in row.cells.items():
                if (
                    mc.review_reason is ReviewReason.READS_DISAGREE
                    and reg_field in REVIEWABLE_REGISTER_FIELDS
                ):
                    report.disagreements.append(
                        CellRef(pair.page_no, row.row_index, reg_field.value)
                    )
        report.input_tokens += pair.input_tokens
        report.output_tokens += pair.output_tokens
    report.cost_usd = cost_usd(report.input_tokens, report.output_tokens, price_in, price_out)
    return report


# --- loading reads ------------------------------------------------------------------------


def parse_pages(text: str) -> tuple[int, ...]:
    """`--pages` value ("1,2", "2") as sorted unique pack page numbers; ValueError otherwise."""
    pages: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part.isdigit() or int(part) not in PACK_PAGES:
            raise ValueError(f"--pages takes pack pages {PACK_PAGES} separated by commas: {text!r}")
        pages.add(int(part))
    return tuple(sorted(pages))


def load_ground_truth(directory: Path = GROUND_TRUTH_DIR) -> dict[int, PageRead]:
    return {
        p: GroundTruthPage.model_validate_json((directory / f"register_p{p}.json").read_bytes())
        for p in PACK_PAGES
    }


def _image(page_no: int, pack_dir: Path) -> bytes:
    return (pack_dir / f"t4_register_page{page_no}.jpg").read_bytes()


def _read_pages(
    client: ModelClient, pages: tuple[int, ...], pack_dir: Path, prompt_version: str
) -> list[ReadPair]:
    schema = load_read_schema(prompt_version)
    pairs: list[ReadPair] = []
    for page_no in pages:
        image = _image(page_no, pack_dir)
        reads: dict[ReadVariant, PageRead] = {}
        tokens_in = tokens_out = 0
        for variant in (ReadVariant.A, ReadVariant.B):
            raw = client.read_page(
                image=image,
                mime="image/jpeg",
                variant=variant,
                instructions=load_prompt(variant, prompt_version),
                schema=schema,
            )
            reads[variant] = parse_read(raw.payload)
            tokens_in += raw.input_tokens
            tokens_out += raw.output_tokens
        pairs.append(
            ReadPair(
                page_no=page_no,
                page_sha256=sha256(image).hexdigest(),
                read_a=reads[ReadVariant.A],
                read_b=reads[ReadVariant.B],
                input_tokens=tokens_in,
                output_tokens=tokens_out,
            )
        )
    return pairs


def load_cached_reads(
    pages: tuple[int, ...],
    *,
    cache_dir: Path = CACHE_DIR,
    pack_dir: Path = PACK_DIR,
    model: str,
    prompt_version: str,
) -> list[ReadPair]:
    """The committed reads of `pages` (research R10); CacheEmpty when any is missing."""
    if not any(cache_dir.glob("*.json")):
        raise CacheEmpty(f"no cached reads in {cache_dir}")
    client = ReplayModelClient(cache_dir, model=model, prompt_version=prompt_version)
    try:
        return _read_pages(client, pages, pack_dir, prompt_version)
    except KeyError as exc:
        raise CacheEmpty(f"{exc.args[0] if exc.args else exc} in {cache_dir}") from exc


Loader = Callable[[tuple[int, ...], bool], list[ReadPair]]


def default_loader(pages: tuple[int, ...], live: bool) -> list[ReadPair]:
    settings = get_settings()
    if not live:
        return load_cached_reads(
            pages, model=settings.EXTRACT_MODEL, prompt_version=settings.PROMPT_VERSION
        )
    if settings.OPENAI_API_KEY is None:
        raise RuntimeError("--live needs OPENAI_API_KEY in apps/api/.env (never printed)")
    client = OpenAIModelClient(model=settings.EXTRACT_MODEL)
    return _read_pages(client, pages, PACK_DIR, settings.PROMPT_VERSION)


# --- CLI ----------------------------------------------------------------------------------


def _price(text: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"not a price: {text!r}") from exc
    if not value.is_finite() or value < 0:
        raise argparse.ArgumentTypeError(f"not a price: {text!r}")
    return value


def _pages_arg(text: str) -> tuple[int, ...]:
    try:
        return parse_pages(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.eval_extraction",
        description="Score merged register reads against the ground truth (F-03.AC6).",
    )
    parser.add_argument("--live", action="store_true", help="read afresh with the model")
    parser.add_argument("--pages", type=_pages_arg, default=PACK_PAGES, help="e.g. 1,2 or 1")
    parser.add_argument("--price-in", type=_price, help="US$ per million input tokens")
    parser.add_argument("--price-out", type=_price, help="US$ per million output tokens")
    parser.add_argument("--json", action="store_true", help="print the JSON report only")
    return parser


def _pct(score: Score) -> str:
    acc = score.accuracy
    return "n/a" if acc is None else f"{float(acc) * 100:.2f}% ({score.correct}/{score.total})"


def format_text(report: EvalReport) -> str:
    lines = [
        f"pages            {', '.join(str(p) for p in report.pages)}",
        f"merged accuracy  {_pct(report.merged)}  (gate >= 98%)",
        f"read A accuracy  {_pct(report.read_a)}",
        f"read B accuracy  {_pct(report.read_b)}",
        f"corrected        {_pct(report.corrected)}",
        f"guessed          {len(report.guessed)}  (gate = 0)",
        f"disagreements    {len(report.disagreements)}  (reviewable fields)",
        f"tokens           in {report.input_tokens}, out {report.output_tokens}",
        "cost             "
        + (
            "n/a (pass --price-in and --price-out)"
            if report.cost_usd is None
            else f"US${report.cost_usd:.6f}"
        ),
    ]
    for m in report.mismatches:
        lines.append(
            f"  miss p{m.page_no} r{m.row_index:02} {m.field}: expected {m.expected_status} "
            f"{m.expected_value!r} struck {m.expected_struck!r}; got {m.got_status} "
            f"{m.got_value!r} struck {m.got_struck!r}"
        )
    for g in report.guessed:
        lines.append(f"  GUESSED p{g.page_no} r{g.row_index:02} {g.field}")
    for d in report.disagreements:
        lines.append(f"  disagree p{d.page_no} r{d.row_index:02} {d.field}")
    lines.append("PASS" if report.passed else "FAIL")
    return "\n".join(lines)


def main(
    argv: Sequence[str] | None = None,
    *,
    loader: Loader = default_loader,
    ground_truth: Mapping[int, PageRead] | None = None,
) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else list(argv))
    try:
        pairs = loader(args.pages, args.live)
    except CacheEmpty as exc:
        print(
            f"eval: {exc}. The pack's reads are not captured yet; run "
            "`uv run python -m scripts.seed_cache --capture` (needs OPENAI_API_KEY) "
            "or pass --live.",
            file=sys.stderr,
        )
        return EXIT_ERROR
    except RuntimeError as exc:
        print(f"eval: {exc}", file=sys.stderr)
        return EXIT_ERROR
    gt = load_ground_truth() if ground_truth is None else ground_truth
    report = evaluate(gt, pairs, price_in=args.price_in, price_out=args.price_out)
    print(json.dumps(report.as_json(), indent=2) if args.json else format_text(report))
    return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
