"""006-web carry-overs from 004: batched Repo reads behind EP-04 and EP-05 (NFR-02).

Contract tests over every Repo implementation (`repo` fixture); the same tests run against
`SupabaseRepo` in `test_phase5_repo_integration.py`.
"""

from app.core.db import Repo
from app.domain.enums import ReadVariant
from tests.factories import make_extraction, sha

# --- EP-05: extractions of many pages in one read ---------------------------------------------


def test_list_extractions_returns_every_read_of_the_pages_for_model_and_prompt(
    repo: Repo,
) -> None:
    wanted = [
        make_extraction(page_sha256=sha(7), read_variant=ReadVariant.B),
        make_extraction(page_sha256=sha(7), read_variant=ReadVariant.A),
        make_extraction(page_sha256=sha(8), read_variant=ReadVariant.A),
    ]
    others = [
        make_extraction(page_sha256=sha(7), prompt_version="v2"),
        make_extraction(page_sha256=sha(8), model="model-y"),
        make_extraction(page_sha256=sha(9)),
    ]
    for e in [*wanted, *others]:
        repo.put_extraction(e)
    got = repo.list_extractions((sha(8), sha(7)), "model-x", "v1")
    assert got == sorted(wanted, key=lambda e: (e.page_sha256, e.read_variant.value))
    assert repo.list_extractions((sha(10),), "model-x", "v1") == []
    assert repo.list_extractions((), "model-x", "v1") == []
