"""Self-tests of the number-traceability helper (005 T010, research R7)."""

import pytest

from tests.support.pack_run import PackRun
from tests.support.trace import (
    allowed_counts,
    allowed_numbers,
    assert_traced,
    counts_in,
    numbers_in,
)

GOLDEN_BEFORE_REVIEW = """Sep 2026 receiving: 29 GRNs, 21 match, 8 need attention

ACT ON THESE
1. 4421 stretch film, 75 ROLL: in register + sheet, NOT in ERP
2. 4430 cartons 5-ply, 1900 PCS: ERP only, dated 29 Sep, keyed 2 Oct by admin2

SOURCES DISAGREE
3. 4417 qty: ERP 46 CTN, register + sheet 48 CTN
4. 4425 qty: sheet 1200 M, register 1200 YD = ERP 1097.28 M
5. 4412 lot: ERP L2609-021, register + sheet L2609-012
6. 4415 date: ERP 13 Sep (Sun), register + sheet 12 Sep

WAITING ON REVIEW
7. 4428 qty unreadable on register (ink blot)
8. 4419 qty corrected on register, 60 to 66

Also: GRN 4429 missing everywhere. Sheet lists 4409 twice and one return (-5000 PCS).
Proof: 86 of 86 rows placed once.
Report: https://app.example/runs/7f3c0000-1111-4222-8333-944445555666"""
"""contracts/telegram-message.md, the pack finalised before review."""


def test_numbers_in_plain_text_tokens() -> None:
    assert numbers_in("qty -5000 PCS, 1097.28 M, lot L2609-012") == [
        "-5000", "1097.28", "2609", "-012"
    ]  # fmt: skip


def test_numbers_in_drops_final_report_line_and_list_ordinals() -> None:
    text = "1. 4421 film\n12. 4417 qty: 46\nReport: https://x.example/runs/1234"
    assert numbers_in(text) == ["4421", "4417", "46"]
    assert numbers_in("Report: 5\nProof: 86 of 86") == ["5", "86", "86"]  # only a final line


def test_numbers_in_html_skips_opaque_elements_tags_and_attributes() -> None:
    html = (
        '<html><head><style>p{margin:4px}</style></head><body class="w-2">'
        '<h1>Sep 2026</h1><p data-trace="opaque">run 7f3c-99 <b>sha 1234</b></p>'
        '<img data-trace="opaque" src="a1.png"><td>4417</td>'
        "<a href='/r/77'>48&nbsp;CTN</a>&#52;&#55;<script>var x = 3;</script></body></html>"
    )
    assert numbers_in(html) == ["2026", "4417", "48", "47"]


def test_allowed_numbers_cover_the_golden_message(pack_run: PackRun) -> None:
    allowed = allowed_numbers(pack_run.repo, pack_run.run_id)
    tokens = numbers_in(GOLDEN_BEFORE_REVIEW)
    assert {"1097.28", "-5000", "2609", "-012", "86", "29", "21", "8", "60", "66"} <= set(tokens)
    assert [t for t in tokens if t not in allowed] == []


def test_allowed_numbers_reject_invented_numbers(pack_run: PackRun) -> None:
    allowed = allowed_numbers(pack_run.repo, pack_run.run_id)
    for invented in ("123456", "4499", "1097.3", "-48"):
        assert invented not in allowed


def test_numbers_in_html_opaque_element_with_implied_end_tags() -> None:
    """An unclosed <li>/<p>/<td> inside an opaque element must not hide the rest."""
    html = '<ul data-trace="opaque"><li>abc</ul><p>4499 invented</p>'
    assert numbers_in(html) == ["4499"]
    assert numbers_in('<div data-trace="opaque"><p>7<td>8</div>12') == ["12"]


def test_counts_in_finds_tokens_next_to_count_words() -> None:
    assert counts_in(GOLDEN_BEFORE_REVIEW) == ["29", "21", "8", "86", "86"]
    html = "<p>3 receipts: 2&nbsp;match</p><td>4417</td><td>rows</td> and +4 more"
    assert counts_in(html) == ["3", "2", "4"]  # one text node: table cells are not phrases


def test_allowed_counts_are_the_run_counts(pack_run: PackRun) -> None:
    counts = allowed_counts(pack_run.repo, pack_run.run_id)
    assert "21" in counts["match"] and "23" not in counts["match"]
    assert "8" in counts["need attention"] and "9" not in counts["need attention"]
    assert "29" in counts["grn"] and "29" in counts["receipt"]
    assert "86" in counts["row"] and "86" in counts["placed"]
    assert counts["more"] == set()  # 8 numbered items: no "+N more"
    assert {"4421", "1097.28"}.isdisjoint(set().union(*counts.values()))


def test_assert_traced_passes_the_golden_message(pack_run: PackRun) -> None:
    assert_traced(GOLDEN_BEFORE_REVIEW, pack_run.repo, pack_run.run_id)


@pytest.mark.parametrize(
    ("original", "mutated"),
    [
        ("21 match", "23 match"),  # a count error
        ("1. 4421 stretch", "1. 4499 stretch"),  # an invented GRN
        ("8 need attention", "9 need attention"),
        ("86 of 86 rows", "86 of 87 rows"),
    ],
)
def test_assert_traced_catches_mutations(pack_run: PackRun, original: str, mutated: str) -> None:
    assert original in GOLDEN_BEFORE_REVIEW
    with pytest.raises(AssertionError):
        assert_traced(
            GOLDEN_BEFORE_REVIEW.replace(original, mutated), pack_run.repo, pack_run.run_id
        )


def test_allowed_numbers_are_not_every_small_integer(pack_run: PackRun) -> None:
    """Seconds, row indexes, row ids and raw JSON dumps made every 0 to 31 allowed (R7)."""
    allowed = allowed_numbers(pack_run.repo, pack_run.run_id)
    assert not {str(n) for n in range(32)} <= allowed
    assert {"31", "59"}.isdisjoint(allowed)  # XLS-r31, no minutes or seconds of rows
