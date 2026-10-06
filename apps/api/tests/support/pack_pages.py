"""The pack's register pages through `extract_page`, for `pack_pages` (conftest, T026) and
tests/test_F03_pack.py (T027). Each source keeps the reads it merged as eval `ReadPair`s, so the
F-03.AC6 test scores exactly what was extracted with `scripts.eval_extraction.evaluate`."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid5

from app.core.config import Settings
from app.core.db import Repo
from app.core.memory_repo import MemoryRepo
from app.domain.enums import ReadVariant
from app.domain.models import Extraction, PageExtractionResult, PageRead
from app.pipeline.extract import cache_file_name, extract_page, load_cached_extractions
from app.pipeline.model_client import ModelClient
from scripts.eval_extraction import ReadPair
from tests.factories import T0, make_run
from tests.support.extracted import page_image, replay_client
from tests.support.pack import PACK_MONTH, PAGES

Source = Literal["cache", "live"]

PACK_PAGES_RUN_ID = UUID("00000000-0000-4000-8000-0000000003f0")


@dataclass(frozen=True)
class PackPages:
    """`extract_page` results for pack pages 1 and 2 (`pages[1]`, `pages[2]`) plus the reads
    behind them."""

    source: Source
    results: Mapping[int, PageExtractionResult]
    pairs: list[ReadPair] = field(default_factory=list)

    def __getitem__(self, page_no: int) -> PageExtractionResult:
        return self.results[page_no]


def offline_settings() -> Settings:
    """Committed defaults (no .env): the `EXTRACT_MODEL` and `PROMPT_VERSION` a CI run uses."""
    return Settings(_env_file=None)


def _no_sleep(_: float) -> None:
    return None


def extract_pack(
    source: Source, *, repo: Repo, client: ModelClient, settings: Settings, pack_dir: Path
) -> PackPages:
    """Extract both pack pages into `repo` (a run is created there) and collect their reads."""
    repo.create_run(make_run(id=PACK_PAGES_RUN_ID, month=PACK_MONTH))
    results: dict[int, PageExtractionResult] = {}
    pairs: list[ReadPair] = []
    for p in PAGES:
        result = extract_page(
            repo=repo,
            client=client,
            run_id=PACK_PAGES_RUN_ID,
            run_month=PACK_MONTH,
            page_no=p,
            image=page_image(pack_dir, p),
            settings=settings,
            sleep=_no_sleep,
        )
        results[p] = result
        records = {
            v: repo.get_extraction(
                result.page.page_sha256, v, settings.EXTRACT_MODEL, settings.PROMPT_VERSION
            )
            for v in ReadVariant
        }
        a, b = records[ReadVariant.A], records[ReadVariant.B]
        assert a is not None and b is not None, "extract_page stores both reads"
        pairs.append(
            ReadPair(
                page_no=p,
                page_sha256=result.page.page_sha256,
                read_a=a.result,
                read_b=b.result,
                input_tokens=a.input_tokens + b.input_tokens,
                output_tokens=a.output_tokens + b.output_tokens,
            )
        )
    return PackPages(source=source, results=results, pairs=pairs)


def cached_pack(cache_dir: Path, pack_dir: Path, settings: Settings | None = None) -> PackPages:
    """The `cache` source: `cache_dir` loaded into a fresh MemoryRepo with
    `load_cached_extractions`, then extracted with a ReplayModelClient narrowed to the current
    model and prompt version (a miss surfaces as the replay `KeyError`, never a live call)."""
    s = offline_settings() if settings is None else settings
    repo = MemoryRepo()
    load_cached_extractions(repo, cache_dir)
    client = replay_client(cache_dir, s)
    return extract_pack("cache", repo=repo, client=client, settings=s, pack_dir=pack_dir)


def write_cache_files(
    cache_dir: Path,
    pack_dir: Path,
    reads: Mapping[int, tuple[PageRead, PageRead]],
    settings: Settings | None = None,
) -> list[Path]:
    """Write `reads` as full `Extraction` files named like committed reads (research R10).
    For tests in `tmp_path` only: the committed cache holds real captures alone."""
    import hashlib

    s = offline_settings() if settings is None else settings
    written: list[Path] = []
    for p, pair in reads.items():
        sha = hashlib.sha256(page_image(pack_dir, p)).hexdigest()
        for variant, read in zip(ReadVariant, pair, strict=True):
            record = Extraction(
                id=uuid5(PACK_PAGES_RUN_ID, f"{sha}.{variant.value}"),
                page_sha256=sha,
                read_variant=variant,
                model=s.EXTRACT_MODEL,
                prompt_version=s.PROMPT_VERSION,
                result=read,
                input_tokens=1,
                output_tokens=1,
                latency_ms=1,
                created_at=T0,
            )
            path = Path(cache_dir) / cache_file_name(
                sha, variant, s.EXTRACT_MODEL, s.PROMPT_VERSION
            )
            path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
            written.append(path)
    return written
