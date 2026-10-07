"""The pack's register pages through `extract_page`, for `pack_pages` (conftest, T026),
tests/test_F03_pack.py (T027) and the golden test's `cache` source (tests/support/extracted.py).
Each source keeps the reads it merged as eval `ReadPair`s, so the F-03.AC6 test scores exactly
what was extracted with `scripts.eval_extraction.evaluate`.

`cached_pack` loads the cache directory with `load_cached_extractions`, which validates EVERY
`*.json` there (any model or prompt version), so stale committed files must stay valid
`Extraction` records."""

import hashlib
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid5

import pytest

from app.core.config import Settings
from app.core.db import Repo
from app.core.memory_repo import MemoryRepo
from app.domain.enums import ReadVariant
from app.domain.models import Extraction, PageExtractionResult, PageRead
from app.pipeline.extract import cache_file_name, extract_page, load_cached_extractions
from app.pipeline.model_client import ModelClient, ReplayModelClient
from scripts.eval_extraction import ReadPair
from scripts.seed_cache import page_image
from tests.factories import T0, make_run
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


def make_settings_hermetic(mp: pytest.MonkeyPatch) -> None:
    """Hide every Settings field from the OS env and .env (undone with `mp`) and clear the
    cached `get_settings`; callers clear it again once `mp` is undone. The autouse
    `hermetic_settings` fixture (conftest) applies it to every test."""
    from app.core.config import get_settings

    for name in Settings.model_fields:
        mp.delenv(name, raising=False)
    mp.setitem(Settings.model_config, "env_file", None)
    get_settings.cache_clear()


@contextmanager
def hermetic_settings_context() -> Iterator[None]:
    """`make_settings_hermetic` for a module-scoped fixture, which runs before the autouse
    `hermetic_settings`: the OS env and .env change neither the cache files matched nor the
    clock."""
    from app.core.config import get_settings

    try:
        with pytest.MonkeyPatch.context() as mp:
            make_settings_hermetic(mp)
            yield
    finally:
        get_settings.cache_clear()


def offline_settings() -> Settings:
    """Committed defaults (no .env): the `EXTRACT_MODEL` and `PROMPT_VERSION` a CI run uses."""
    return Settings(_env_file=None)


def _no_sleep(_: float) -> None:
    """For the `cache` source: no calls are made, so there is nothing to back off from."""
    return None


def replay_client(cache_dir: Path, settings: Settings | None = None) -> ReplayModelClient:
    """A replay client narrowed to the current `EXTRACT_MODEL` and `PROMPT_VERSION`."""
    s = offline_settings() if settings is None else settings
    return ReplayModelClient(cache_dir, model=s.EXTRACT_MODEL, prompt_version=s.PROMPT_VERSION)


def extract_pack(
    source: Source,
    *,
    repo: Repo,
    client: ModelClient,
    settings: Settings,
    pack_dir: Path,
    run_id: UUID = PACK_PAGES_RUN_ID,
    sleep: Callable[[float], None] | None = None,
) -> PackPages:
    """Extract both pack pages into `repo` (run `run_id` is created there) and collect their
    reads. `sleep` reaches `extract_page` only when given: the `cache` source passes a no-op,
    `live` keeps `extract_page`'s default (`time.sleep`), so its retries really back off."""
    repo.create_run(make_run(id=run_id, month=PACK_MONTH))
    backoff: dict[str, Any] = {} if sleep is None else {"sleep": sleep}
    results: dict[int, PageExtractionResult] = {}
    pairs: list[ReadPair] = []
    for p in PAGES:
        result = extract_page(
            repo=repo,
            client=client,
            run_id=run_id,
            run_month=PACK_MONTH,
            page_no=p,
            image=page_image(pack_dir, p),
            settings=settings,
            **backoff,
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


def cached_pack(
    cache_dir: Path,
    pack_dir: Path,
    settings: Settings | None = None,
    run_id: UUID = PACK_PAGES_RUN_ID,
) -> PackPages:
    """The `cache` source: `cache_dir` loaded into a fresh MemoryRepo with
    `load_cached_extractions`, then extracted with a ReplayModelClient narrowed to the current
    model and prompt version (a miss surfaces as the replay `KeyError`, never a live call).

    Raises what `extract_page` raises: an unreadable page as `PageUnreadable` (FM-06)."""
    s = offline_settings() if settings is None else settings
    repo = MemoryRepo()
    load_cached_extractions(repo, cache_dir)
    client = replay_client(cache_dir, s)
    return extract_pack(
        "cache",
        repo=repo,
        client=client,
        settings=s,
        pack_dir=pack_dir,
        run_id=run_id,
        sleep=_no_sleep,
    )


def write_cache_files(
    cache_dir: Path,
    pack_dir: Path,
    reads: Mapping[int, tuple[PageRead, PageRead]],
    settings: Settings | None = None,
) -> list[Path]:
    """Write `reads` (page number → (read A, read B)) as full `Extraction` files for
    `settings`' model and prompt version, named like committed reads (research R10). For tests
    in a scratch directory only: the committed cache holds real captures alone."""
    s = offline_settings() if settings is None else settings
    written: list[Path] = []
    for p, pair in reads.items():
        sha = hashlib.sha256(page_image(pack_dir, p)).hexdigest()
        for variant, read in zip(ReadVariant, pair, strict=True):
            record = Extraction(
                id=uuid5(
                    PACK_PAGES_RUN_ID,
                    cache_file_name(sha, variant, s.EXTRACT_MODEL, s.PROMPT_VERSION),
                ),
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
