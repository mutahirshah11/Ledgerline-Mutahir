"""Number traceability for the report and the summary message (005 research R7; F-09 AC2,
F-10 AC5): every number a reader sees must be derivable from the run's stored data.

`numbers_in(text)` lists the number tokens (`-?\\d+(?:\\.\\d+)?`) of the visible text: HTML tags,
attributes, `<script>`/`<style>` and elements marked `data-trace="opaque"` (hashes, run id, URLs)
are skipped, entities decoded; a final `Report:` line (the message's link) is dropped, and so
are list ordinals (`7. ` at a line start: layout, not data) and source row ids (`REG-p2-r14`,
`ERP-0028`, `XLS-r31`: ids, not numbers). `allowed_numbers(repo, run_id)` is the set of tokens
the templates can print from the run (R7, in the forms contracts/telegram-message.md and
contracts/frozen-files.md print them).

Membership alone cannot catch a wrong count (small integers are also days, quantities, ...), so
`counts_in(text)` lists the tokens written next to a count word (`21 match`, `8 need attention`,
`29 GRNs`, `receipts`, `86 of 86 rows`, `placed`, `+3 more`) and `allowed_counts(repo, run_id)`
gives, per count word, the counts that word may carry. `assert_traced(text, repo, run_id)` runs
both checks; F-09 AC2 and F-10 AC5 tests call it.
"""

import re
from collections import Counter
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
from html.parser import HTMLParser
from uuid import UUID

from app.core import clock
from app.core.db import Repo
from app.domain.enums import Outcome
from app.domain.models import Cell, CellContext, FlagAckContext, ReviewItem, RowInOneReadContext
from app.pipeline.engine import open_review_count
from app.services.stages import run_outcome_counts

NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_OPAQUE_IDS = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{64}"
)
"""UUIDs and SHA-256 hex inside stored values are ids, not numbers a reader may be shown."""
_ROW_IDS = re.compile(r"\b(?:REG-p\d+-r\d+|ERP-\d+|XLS-r\d+)\b")
"""Source row ids (`app.domain.ids`): printed in the report, but ids, not numbers."""
_ORDINAL = re.compile(r"^\s*\d+\.\s", re.MULTILINE)
_COUNT_WORD = re.compile(
    r"(?<![\w.])\+?(\d+)(?:[ \t\xa0]+of[ \t\xa0]+(\d+))?[ \t\xa0]+"
    r"(match|need attention|GRNs?|receipts?|rows?|placed|more)\b",
    re.IGNORECASE,
)
"""A count and its word in one text node (`86 of 86 rows`: both numbers count rows)."""
_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
)
_HIDDEN = frozenset({"script", "style", "title"})
MESSAGE_ITEMS = 8
"""Numbered items the summary message prints before `+N more` (telegram-message.md)."""


class _VisibleText(HTMLParser):
    """Collects text outside opaque, script and style elements (one chunk per text node)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self._skip: list[str] = []  # open skipped elements, innermost last

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _VOID:
            return
        if self._skip or ("data-trace", "opaque") in attrs or tag in _HIDDEN:
            self._skip.append(tag)

    def handle_endtag(self, tag: str) -> None:
        # Pop through the last open `tag`: elements left open inside it (`<li>`, `<p>`, `<td>`
        # with implied end tags) close with it, so the rest of the document is not skipped.
        if tag in self._skip:
            del self._skip[len(self._skip) - 1 - self._skip[::-1].index(tag) :]

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.chunks.append(data)


def _visible_chunks(text: str) -> list[str]:
    lines = text.rstrip().splitlines()
    if lines and lines[-1].startswith("Report:"):
        lines = lines[:-1]
    parser = _VisibleText()
    parser.feed("\n".join(lines))
    parser.close()
    return [_ROW_IDS.sub(" ", chunk) for chunk in parser.chunks]


def numbers_in(text: str) -> list[str]:
    """The number tokens of the visible `text` (HTML or plain), in order (see module doc)."""
    visible = _ORDINAL.sub(" ", " ".join(_visible_chunks(text)))
    return NUMBER.findall(visible)


def _count_matches(text: str) -> list[tuple[str, list[str]]]:
    out: list[tuple[str, list[str]]] = []
    for chunk in _visible_chunks(text):
        for m in _COUNT_WORD.finditer(chunk):
            out.append((_word_key(m.group(3)), [n for n in m.group(1, 2) if n is not None]))
    return out


def _word_key(word: str) -> str:
    word = word.lower()
    return word[:-1] if word in {"grns", "receipts", "rows"} else word


def counts_in(text: str) -> list[str]:
    """The tokens of the visible `text` written next to a count word, in order."""
    return [n for _, numbers in _count_matches(text) for n in numbers]


def _tokens(values: Iterable[object]) -> set[str]:
    out: set[str] = set()
    for value in values:
        if value is not None:
            out.update(NUMBER.findall(_OPAQUE_IDS.sub(" ", str(value))))
    return out


def _decimal(value: Decimal | None) -> set[str]:
    """As recorded and without trailing zeros (`1200.00` → `1200`, never `1.2E+3`)."""
    return set() if value is None else {str(value), f"{value.normalize():f}"}


def _day(value: date | datetime | None) -> set[str]:
    """A row date or `entered_at` as templates print it: `{d} {Mon}` (+ year); `entered_at`
    in the scenario zone."""
    if value is None:
        return set()
    if isinstance(value, datetime):
        value = value.astimezone(clock.KARACHI)
    return {str(value.day), str(value.year)}


def _timestamp(value: datetime | None) -> set[str]:
    """A printed instant (finalised_at, scenario clock, review time) in Asia/Karachi: day,
    month and year (plain, two-digit and ISO date tokens) and hour and minute; no seconds."""
    if value is None:
        return set()
    t = value.astimezone(clock.KARACHI)
    out = {str(t.year), str(t.month), f"{t.month:02d}", str(t.day), f"{t.day:02d}"}
    out |= _tokens([t.date().isoformat()]) | {str(t.hour), f"{t.hour:02d}", f"{t.minute:02d}"}
    return out


def _cell(cell: Cell | None) -> set[str]:
    return set() if cell is None else _tokens([cell.value, cell.struck, *cell.candidates])


def _item(item: ReviewItem) -> set[str]:
    """What the review templates print: GRN, reads, struck value, candidates, notes, the page
    of a row read once, flag details; the decision's value, note and time."""
    out = _tokens([item.grn_no, item.resolved_value, item.note]) | _timestamp(item.resolved_at)
    out |= _cell(item.read_a) | _cell(item.read_b)
    ctx = item.context
    if isinstance(ctx, CellContext):
        out |= _tokens([ctx.page_no, ctx.struck, ctx.note, *ctx.candidates])
    elif isinstance(ctx, RowInOneReadContext):
        out |= _tokens([ctx.page_no]).union(*(_cell(c) for c in ctx.cells.values()))
    elif isinstance(ctx, FlagAckContext):
        out |= _tokens(ctx.detail.values())
    return out


def allowed_counts(repo: Repo, run_id: UUID) -> dict[str, set[str]]:
    """Per count word (`match`, `need attention`, `grn`, `receipt`, `row`, `placed`, `more`),
    the counts it may carry: matched / need-attention groups (the headline), open review items,
    group and C5 GRN counts, row counts (C1 in/placed per source, C2 rows and placed, unplaced,
    placed twice) and the `+N more` figure."""
    groups = repo.list_groups(run_id)
    items = repo.list_review_items(run_id)
    proof = repo.get_proof(run_id)
    counts = run_outcome_counts(repo, run_id)
    matched = counts[Outcome.MATCHED] + counts[Outcome.MATCHED_AFTER_CONVERSION]
    attention = len(groups) - matched
    ungrouped = sum(1 for i in items if i.grn_no is None)
    receipts: set[object] = {len(groups)}
    rows: set[object] = {len(repo.list_source_rows(run_id))}
    placed: set[object] = set()
    if proof is not None:
        c = proof.checks
        receipts.add(c.C5.grns)
        placed |= {c.C2.rows - len(c.C2.unplaced), *(s.placed for s in c.C1.per_source.values())}
        rows |= placed | {c.C2.rows, len(c.C2.unplaced), len(c.C2.placed_twice)}
        rows |= {s.in_ for s in c.C1.per_source.values()} | set(c.C3.per_source.values())
    more = {n - MESSAGE_ITEMS for n in (attention, attention + ungrouped) if n > MESSAGE_ITEMS}
    return {
        "match": _tokens([matched, counts[Outcome.MATCHED]]),
        "need attention": _tokens([attention, open_review_count(repo, run_id)]),
        "grn": _tokens(receipts),
        "receipt": _tokens(receipts),
        "row": _tokens(rows),
        "placed": _tokens(placed),
        "more": _tokens(more),
    }


def allowed_numbers(repo: Repo, run_id: UUID) -> set[str]:
    """Every number token the templates print from the stored run (research R7): the counts
    above and outcome counts; the run month; finalised time and scenario clock; GRNs; recorded
    qty and qty_base; day and year of every row date and `entered_at`; digit groups of lots,
    items, suppliers and users; diff values; flag details; review reads, values and notes;
    proof figures; normalisation log before/after and entries per code; input file names and
    sizes; attempts."""
    run = repo.get_run(run_id)
    out = set().union(*allowed_counts(repo, run_id).values())
    out |= _tokens([*run_outcome_counts(repo, run_id).values(), run.month])
    out |= _timestamp(run.finalised_at) | _timestamp(run.scenario_now)
    for r in repo.list_source_rows(run_id):
        out |= _decimal(r.qty) | _decimal(r.qty_base) | _day(r.date) | _day(r.meta.entered_at)
        out |= _tokens([r.grn_no, r.item, r.lot_no, r.supplier_code, r.uom, r.meta.page_no])
        out |= _tokens([r.meta.entered_by, r.meta.received_by, r.meta.remarks])
    for g in repo.list_groups(run_id):
        out |= _tokens([g.grn_no])
        for d in g.diffs:
            out |= _tokens([*d.values.values(), *d.compared.values()])
    for f in repo.list_flags(run_id):
        out |= _tokens([f.grn_no, *f.detail.values()])
    for i in repo.list_review_items(run_id):
        out |= _item(i)
    norm_log = repo.list_norm_log(run_id)
    for n in norm_log:
        out |= _tokens([n.before, n.after])
    out |= _tokens(Counter(n.code for n in norm_log).values())  # report: entries per code
    for sf in repo.list_source_files(run_id):
        out |= _tokens([sf.page_no, sf.filename, sf.size_bytes])
    for note in repo.list_notifications(run_id):
        out |= _tokens([note.attempts])
    proof = repo.get_proof(run_id)
    if proof is not None:
        c = proof.checks
        out |= _tokens([*c.C4.qty_unknown, *c.C3.problems, *c.C5.gaps])
        for balance in c.C4.by_source_unit.values():
            out |= _decimal(balance.in_) | _decimal(balance.placed)
    return out


def assert_traced(text: str, repo: Repo, run_id: UUID) -> None:
    """F-09 AC2 / F-10 AC5: every visible number of `text` is in `allowed_numbers`, and every
    number next to a count word is a count that word may carry (`allowed_counts`)."""
    allowed = allowed_numbers(repo, run_id)
    untraced = [n for n in numbers_in(text) if n not in allowed]
    assert untraced == [], f"numbers not derivable from the run: {untraced}"
    counts = allowed_counts(repo, run_id)
    wrong = [(w, n) for w, ns in _count_matches(text) for n in ns if n not in counts[w]]
    assert wrong == [], f"counts not derivable from the run: {wrong}"
