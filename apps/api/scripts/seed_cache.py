"""Capture and load the committed pack reads (T031; contracts/extract-service.md §5-§6, research
R10, FR-030, FR-031).

Usage (from apps/api)::

    uv run python -m scripts.seed_cache --capture   # live: needs OPENAI_API_KEY in apps/api/.env
    uv run python -m scripts.seed_cache --load      # load the committed reads into the repo

`--capture` reads `data/kestrel-2026-09/t4_register_page{1,2}.jpg` with `OpenAIModelClient`
through `extract_page` (validation, retries, merge and readability checks) into a scratch
MemoryRepo, then writes the 4 stored `Extraction` records to `supabase/seed/extractions/` as
`{sha}.{variant}.{model}.{prompt_version}.json` (`cache_file_name`). It refuses, before any model
call, when a file for any of the 4 keys already exists, and writes nothing unless both pages were
read (no partial capture). The files are never edited by hand (Clarification 4).

`--load` loads every committed read into the configured repo with `load_cached_extractions`
(idempotent). Until Phase 3 that repo is a MemoryRepo, so `--load` only validates the files and
reports the count; Phase 3 passes a SupabaseRepo factory.

Exit codes: 0 success; 2 on an error (no key, an existing file, a failed read, a bad cache
file), with one `seed_cache: <message>` line on stderr and no traceback. Never prints the key.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from uuid import uuid4

from app.core.config import Settings, get_settings
from app.core.db import Repo
from app.core.memory_repo import MemoryRepo
from app.domain.enums import ReadVariant, RunStatus
from app.domain.models import Run
from app.pipeline.extract import cache_file_name, extract_page, load_cached_extractions
from app.pipeline.model_client import ModelClient, OpenAIModelClient

SCRIPTS_DIR = Path(__file__).resolve().parent
API_DIR = SCRIPTS_DIR.parent
REPO_ROOT = API_DIR.parent.parent
CACHE_DIR = REPO_ROOT / "supabase" / "seed" / "extractions"
PACK_DIR = REPO_ROOT / "data" / "kestrel-2026-09"
PACK_PAGES = (1, 2)
PACK_MONTH = "2026-09"
CAPTURE_USER = "seed_cache@ledgerline.local"

_API_KEY = re.compile(r"sk-[A-Za-z0-9_\-*]+")

EXIT_OK = 0
EXIT_ERROR = 2

ClientFactory = Callable[[Settings], ModelClient]
RepoFactory = Callable[[], Repo]


def _image(pack_dir: Path, page_no: int) -> bytes:
    return (pack_dir / f"t4_register_page{page_no}.jpg").read_bytes()


def _scratch_repo() -> tuple[MemoryRepo, Run]:
    """A MemoryRepo holding one throwaway run: `extract_page` queues review items on a run."""
    from app.core import clock

    repo = MemoryRepo()
    at = clock.now()
    run = repo.create_run(
        Run(
            id=uuid4(),
            month=PACK_MONTH,
            status=RunStatus.CREATED,
            run_key=None,
            failed_stage=None,
            error_message=None,
            scenario_now=at,
            created_by=CAPTURE_USER,
            created_at=at,
            finalised_at=None,
            finalised_by=None,
        )
    )
    return repo, run


def capture(
    *,
    client: ModelClient,
    settings: Settings,
    cache_dir: Path = CACHE_DIR,
    pack_dir: Path = PACK_DIR,
) -> list[Path]:
    """Read both pack pages with `client` and write their 4 reads to `cache_dir`.

    Raises:
        FileExistsError: a file for one of the 4 keys exists (checked before any call).
        UpstreamFailed / PageUnreadable / ValueError: from `extract_page`; nothing is written.
    """
    images = {p: _image(pack_dir, p) for p in PACK_PAGES}
    shas = {p: hashlib.sha256(image).hexdigest() for p, image in images.items()}
    model, version = settings.EXTRACT_MODEL, settings.PROMPT_VERSION
    targets = {
        (p, v): cache_dir / cache_file_name(shas[p], v, model, version)
        for p in PACK_PAGES
        for v in ReadVariant
    }
    existing = sorted(path.name for path in targets.values() if path.exists())
    if existing:
        raise FileExistsError(f"refusing to overwrite committed reads: {', '.join(existing)}")

    repo, run = _scratch_repo()
    for page_no in PACK_PAGES:
        extract_page(
            repo=repo,
            client=client,
            run_id=run.id,
            run_month=PACK_MONTH,
            page_no=page_no,
            image=images[page_no],
            settings=settings,
        )
    written: list[Path] = []
    for (page_no, variant), path in targets.items():
        record = repo.get_extraction(shas[page_no], variant, model, version)
        assert record is not None, "extract_page stores every read it returns"
        with path.open("x", encoding="utf-8", newline="\n") as fh:  # "x": never overwrite
            fh.write(record.model_dump_json(indent=2) + "\n")
        written.append(path)
    return written


def _default_client(settings: Settings) -> ModelClient:
    assert settings.OPENAI_API_KEY is not None
    return OpenAIModelClient(
        api_key=settings.OPENAI_API_KEY.get_secret_value(), model=settings.EXTRACT_MODEL
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.seed_cache",
        description="Capture (live) or load the committed pack reads (research R10).",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--capture", action="store_true", help="live-read the pack pages")
    mode.add_argument("--load", action="store_true", help="load the committed reads")
    return parser


def _fail(message: str) -> int:
    """One stderr line; anything shaped like an API key is masked."""
    print(f"seed_cache: {_API_KEY.sub('sk-***', message)}", file=sys.stderr)
    return EXIT_ERROR


def main(
    argv: Sequence[str] | None = None,
    *,
    cache_dir: Path = CACHE_DIR,
    pack_dir: Path = PACK_DIR,
    settings: Settings | None = None,
    client_factory: ClientFactory | None = None,
    repo_factory: RepoFactory = MemoryRepo,
) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else list(argv))
    if args.load:
        try:
            count = load_cached_extractions(repo_factory(), cache_dir)
        except (FileNotFoundError, ValueError) as exc:
            return _fail(str(exc))
        hint = "" if count else " (nothing new; run --capture if the cache is empty)"
        print(f"loaded {count} cached reads from {cache_dir}{hint}")
        return EXIT_OK

    cfg = get_settings() if settings is None else settings
    if client_factory is None:
        if cfg.OPENAI_API_KEY is None:
            return _fail("OPENAI_API_KEY is not set (apps/api/.env); --capture needs the model")
        client_factory = _default_client
    try:
        written = capture(
            client=client_factory(cfg), settings=cfg, cache_dir=cache_dir, pack_dir=pack_dir
        )
    except Exception as exc:  # one line, no traceback; never echoes the key
        return _fail(f"{type(exc).__name__}: {exc}")
    print(f"wrote {len(written)} reads to {cache_dir}:")
    for path in written:
        print(f"  {path.name}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
