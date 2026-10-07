"""Register input from page reads, for the golden test's `cache` source (003 FR-035, research
R12). Reads go through the real extract service, on the same path as the pack tests
(`cached_pack` in tests/support/pack_pages.py): `extract_page` with a `ReplayModelClient` on
the committed cache files for the current `EXTRACT_MODEL` and `PROMPT_VERSION` (exactly what a
real run reads), so merging, FM-06 (`check_readable`) and record validation all apply. Each
`MergedPage` is mapped to the engine's `RegisterPageInput`, and the extraction's review items
become decisions, as in a real run (blueprint C23: a row seen by one read carries a
`ROW_IN_ONE_READ` item for C3)."""

import glob
import json
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from uuid import UUID

from app.core.config import Settings
from app.domain.enums import ReadVariant
from app.domain.models import (
    MergedPage,
    PageExtractionResult,
    PageRead,
    RegisterPageInput,
    ReviewItem,
)
from app.pipeline.engine import EngineInputs
from app.pipeline.extract import cache_file_name, parse_read
from scripts.seed_cache import page_image
from tests.support.pack import PAGES, pack_engine_inputs
from tests.support.pack_pages import (
    cached_pack,
    offline_settings,
    replay_client,
    write_cache_files,
)

NO_COMMITTED_READS = (
    "no committed reads in supabase/seed/extractions/; run scripts.seed_cache --capture"
)

PageReads = Mapping[int, tuple[PageRead, PageRead]]
"""Page number → (read A, read B)."""


def committed_read_files(cache_dir: Path, settings: Settings | None = None) -> list[Path]:
    """The committed read files for the current model and prompt version; `.gitkeep` and
    stale files (another model or prompt version) alone mean none."""
    s = offline_settings() if settings is None else settings
    model, version = glob.escape(s.EXTRACT_MODEL), glob.escape(s.PROMPT_VERSION)
    return sorted(
        path
        for v in ReadVariant
        for path in Path(cache_dir).glob(cache_file_name("*", v, model, version))
    )


def cache_skip_reason(cache_dir: Path, settings: Settings | None = None) -> str | None:
    """Why the `cache` source must skip (golden and pack gating), or None when reads for the
    current `EXTRACT_MODEL` and `PROMPT_VERSION` exist."""
    s = offline_settings() if settings is None else settings
    if committed_read_files(cache_dir, s):
        return None
    if any(Path(cache_dir).glob("*.json")):
        return (
            "committed reads are for another model/prompt version "
            f"(expected {s.EXTRACT_MODEL}/{s.PROMPT_VERSION})"
        )
    return NO_COMMITTED_READS


def ground_truth_read(ground_truth_dir: Path, page_no: int) -> PageRead:
    """A ground-truth page as a `PageRead` (validated like a model read)."""
    data = json.loads((ground_truth_dir / f"register_p{page_no}.json").read_text("utf-8"))
    del data["page_no"], data["source"]
    return parse_read(data)


def cached_pack_reads(
    cache_dir: Path, pack_dir: Path, settings: Settings | None = None
) -> PageReads:
    """Reads A and B of each pack page from the committed cache for the current model and
    prompt version, matched by image SHA-256.

    Raises `KeyError` when a page or variant has no committed read (a partial capture is an
    error, not a skip) and `CachedReadAmbiguous` when more than one file matches.
    """
    replay = replay_client(cache_dir, settings)

    def read(image: bytes, variant: ReadVariant) -> PageRead:
        raw = replay.read_page(
            image=image, mime="image/jpeg", variant=variant, instructions="", schema={}
        )
        return parse_read(raw.payload)

    out = {}
    for p in PAGES:
        image = page_image(pack_dir, p)
        out[p] = (read(image, ReadVariant.A), read(image, ReadVariant.B))
    return out


def cached_pack_pages(
    cache_dir: Path, pack_dir: Path, run_id: UUID, settings: Settings | None = None
) -> dict[int, PageExtractionResult]:
    """Each pack page extracted by the real `extract_page` from the committed cache (current
    model and prompt version) for run `run_id`: the `cache` path of `tests/support/pack_pages`
    (`cached_pack`), shared with the pack tests.

    Raises what `extract_page` raises; a missing read surfaces as the replay `KeyError` (a
    partial capture is an error, not a skip), an unreadable page as `PageUnreadable` (FM-06).
    """
    return dict(cached_pack(cache_dir, pack_dir, settings, run_id=run_id).results)


def merged_page_input(page: MergedPage) -> RegisterPageInput:
    """E1 → engine input. Field names differ: `blank_row_slots_a/b` → `blank_slots_a/b`."""
    return RegisterPageInput(
        page_no=page.page_no,
        page_sha256=page.page_sha256,
        month_written=page.month_written,
        row_count_a=page.row_count_a,
        row_count_b=page.row_count_b,
        blank_slots_a=page.blank_row_slots_a,
        blank_slots_b=page.blank_row_slots_b,
        rows=page.rows,
    )


def build_engine_inputs_from_pages(
    pack_dir: Path,
    pages: Mapping[int, PageExtractionResult],
    run_id: UUID,
    decisions: Iterable[ReviewItem] = (),
) -> EngineInputs:
    """Engine inputs for the pack with register pages from `extract_page` results. Decisions
    are the extraction's (open) review items, each replaced by a given decision with the same
    key."""
    register: list[RegisterPageInput] = []
    items: dict[str, ReviewItem] = {}
    for p in PAGES:
        register.append(merged_page_input(pages[p].page))
        for item in pages[p].review_items:
            items[item.item_key] = item
    for decision in decisions:
        items[decision.item_key] = decision
    return pack_engine_inputs(pack_dir, run_id, register, list(items.values()))


def build_engine_inputs_from_reads(
    pack_dir: Path, reads: PageReads, run_id: UUID, decisions: Iterable[ReviewItem] = ()
) -> EngineInputs:
    """Engine inputs for the pack from `reads`, through the same path as the `cache` source:
    the reads are written as replay files (default model and prompt version) and extracted by
    `extract_page`."""
    settings = offline_settings()
    with tempfile.TemporaryDirectory() as tmp:
        write_cache_files(Path(tmp), pack_dir, reads, settings)
        pages = cached_pack_pages(Path(tmp), pack_dir, run_id, settings)
    return build_engine_inputs_from_pages(pack_dir, pages, run_id, decisions)
