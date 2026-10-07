"""Number traceability for the report and the summary message (005 research R7; F-09 AC2,
F-10 AC5): every number a reader sees must be derivable from the run's stored data.

`numbers_in(text)` lists the number tokens (`-?\\d+(?:\\.\\d+)?`) of the visible text: HTML tags,
attributes, `<script>`/`<style>` and elements marked `data-trace="opaque"` (hashes, run id, URLs)
are skipped, entities decoded; a final `Report:` line (the message's link) is dropped, and so
are list ordinals (`7. ` at a line start: layout, not data). `allowed_numbers(repo, run_id)` is
the set of tokens derivable from the run (R7 list); a test asserts `numbers_in(...)` ⊆ it.
"""

import re
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
from html.parser import HTMLParser
from uuid import UUID

from app.core import clock
from app.core.db import Repo
from app.domain.enums import Outcome
from app.pipeline.engine import open_review_count
from app.services.stages import run_outcome_counts

NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_OPAQUE_IDS = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{64}"
)
"""UUIDs and SHA-256 hex inside stored values are ids, not numbers a reader may be shown."""
_ORDINAL = re.compile(r"^\s*\d+\.\s", re.MULTILINE)
_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
)
_HIDDEN = frozenset({"script", "style", "title"})


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
        if self._skip and self._skip[-1] == tag:
            self._skip.pop()

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.chunks.append(data)


def numbers_in(text: str) -> list[str]:
    """The number tokens of the visible `text` (HTML or plain), in order (see module doc)."""
    lines = text.rstrip().splitlines()
    if lines and lines[-1].startswith("Report:"):
        lines = lines[:-1]
    parser = _VisibleText()
    parser.feed("\n".join(lines))
    parser.close()
    visible = _ORDINAL.sub(" ", " ".join(parser.chunks))
    return NUMBER.findall(visible)


def _tokens(values: Iterable[object]) -> set[str]:
    out: set[str] = set()
    for value in values:
        if value is not None:
            out.update(NUMBER.findall(_OPAQUE_IDS.sub(" ", str(value))))
    return out


def _decimal(value: Decimal | None) -> set[str]:
    """As recorded and without trailing zeros (`1200.00` → `1200`, never `1.2E+3`)."""
    return set() if value is None else {str(value), f"{value.normalize():f}"}


def _date(value: date | datetime | None) -> set[str]:
    """Day, month and year (plain and two-digit), the ISO form's tokens and, for a time, its
    hour, minute and second in the scenario zone and as stored."""
    if value is None:
        return set()
    out = {str(value.year), str(value.month), str(value.day), f"{value.month:02d}"}
    out |= {f"{value.day:02d}"} | _tokens([value.isoformat()])
    if isinstance(value, datetime):
        for t in (value, value.astimezone(clock.KARACHI)):
            out |= _date(t.date()) | {str(t.hour), str(t.minute), str(t.second)}
            out |= {f"{t.hour:02d}", f"{t.minute:02d}", f"{t.second:02d}"}
    return out


def allowed_numbers(repo: Repo, run_id: UUID) -> set[str]:
    """Every number token derivable from the stored run (research R7): outcome counts, group
    and need-attention counts, GRNs, qty and qty_base, dates and entered_at, proof figures,
    row counts, digit groups of lots, items, row ids and other recorded text, the run month,
    created and finalised times, review values and notes, page numbers, attempt counts."""
    run = repo.get_run(run_id)
    rows = repo.list_source_rows(run_id)
    groups = repo.list_groups(run_id)
    flags = repo.list_flags(run_id)
    items = repo.list_review_items(run_id)
    files = repo.list_source_files(run_id)
    notifications = repo.list_notifications(run_id)
    proof = repo.get_proof(run_id)
    counts = run_outcome_counts(repo, run_id)
    matched = counts[Outcome.MATCHED] + counts[Outcome.MATCHED_AFTER_CONVERSION]
    figures: list[object] = [
        *counts.values(),
        len(groups),
        matched,
        len(groups) - matched,
        open_review_count(repo, run_id),
        len(rows),
        *(sum(1 for r in rows if r.source is s) for s in {r.source for r in rows}),
        len(items),
        len(flags),
        len(files),
        run.month,
    ]
    out = _tokens(figures) | _date(run.created_at) | _date(run.finalised_at)
    out |= _date(run.scenario_now)
    for r in rows:
        out |= _decimal(r.qty) | _decimal(r.qty_base) | _date(r.date) | _date(r.meta.entered_at)
        out |= _tokens([r.grn_no, r.source_row_id, r.item, r.lot_no, r.supplier_code, r.uom])
        out |= _tokens([r.meta.page_no, r.meta.row_index, r.meta.entered_by, r.meta.remarks])
        out |= _tokens([r.meta.received_by, *r.raw.values()])
    for g in groups:
        out |= _tokens([g.grn_no])
        for d in g.diffs:
            out |= _tokens([*d.values.values(), *d.compared.values()])
    for f in flags:
        out |= _tokens([f.grn_no, f.source_row_id, *f.detail.values()])
    for i in items:
        out |= _tokens([i.grn_no, i.source_row_id, i.resolved_value, i.note])
        out |= _tokens(c.model_dump_json() for c in (i.read_a, i.read_b, i.context) if c)
        out |= _date(i.resolved_at)
    for sf in files:
        out |= _tokens([sf.page_no, sf.filename])
    for n in notifications:
        out |= _tokens([n.attempts]) | _date(n.sent_at)
    if proof is not None:
        out |= _tokens([proof.checks.model_dump_json()]) | _date(proof.computed_at)
    return out
