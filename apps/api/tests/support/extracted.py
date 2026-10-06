"""Register input from page reads, for the golden test's `cache` source (003 FR-035, research
R12): each page's read A and read B are merged by `merge_reads`, the `MergedPage` is mapped to
the engine's `RegisterPageInput`, and the extraction's review items become decisions, as in a
real run (blueprint C23: a row seen by one read carries a `ROW_IN_ONE_READ` item for C3)."""

import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from uuid import UUID

from app.domain.enums import ReadVariant
from app.domain.models import MergedPage, PageRead, RegisterPageInput, ReviewItem
from app.pipeline.engine import EngineInputs
from app.pipeline.extract import build_review_items, merge_reads, parse_read
from app.pipeline.model_client import ReplayModelClient
from tests.support.pack import PACK_MONTH, PAGES, pack_engine_inputs

NO_COMMITTED_READS = (
    "no committed reads in supabase/seed/extractions/; run scripts.seed_cache --capture"
)

PageReads = Mapping[int, tuple[PageRead, PageRead]]
"""Page number → (read A, read B)."""


def page_image(pack_dir: Path, page_no: int) -> bytes:
    return (pack_dir / f"t4_register_page{page_no}.jpg").read_bytes()


def committed_read_files(cache_dir: Path) -> list[Path]:
    """The committed read files (`*.json`); `.gitkeep` alone means none."""
    return sorted(cache_dir.glob("*.json"))


def ground_truth_read(ground_truth_dir: Path, page_no: int) -> PageRead:
    """A ground-truth page as a `PageRead` (validated like a model read)."""
    data = json.loads((ground_truth_dir / f"register_p{page_no}.json").read_text("utf-8"))
    del data["page_no"], data["source"]
    return parse_read(data)


def cached_pack_reads(cache_dir: Path, pack_dir: Path) -> dict[int, tuple[PageRead, PageRead]]:
    """Reads A and B of each pack page from the committed cache, matched by image SHA-256.

    Raises `KeyError` when a page or variant has no committed read (a partial capture is an
    error, not a skip) and `CachedReadAmbiguous` when more than one file matches.
    """
    replay = ReplayModelClient(cache_dir)

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


def build_engine_inputs_from_reads(
    pack_dir: Path, reads: PageReads, run_id: UUID, decisions: Iterable[ReviewItem] = ()
) -> EngineInputs:
    """Engine inputs for the pack with register pages merged from `reads`. Decisions are the
    extraction's (open) review items, each replaced by a given decision with the same key."""
    pages: list[RegisterPageInput] = []
    items: dict[str, ReviewItem] = {}
    for p in PAGES:
        read_a, read_b = reads[p]
        sha = hashlib.sha256(page_image(pack_dir, p)).hexdigest()
        merged = merge_reads(read_a, read_b, page_no=p, page_sha256=sha)
        pages.append(merged_page_input(merged))
        for item in build_review_items(merged, read_a, read_b, run_id=run_id, run_month=PACK_MONTH):
            items[item.item_key] = item
    for decision in decisions:
        items[decision.item_key] = decision
    return pack_engine_inputs(pack_dir, run_id, pages, list(items.values()))
