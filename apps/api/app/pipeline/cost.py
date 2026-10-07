"""Model read cost in US$ (006-web spec.md Clarifications, Session 2026-10-07).

Only new pages cost money; a page already read comes from the extraction cache (F-03.R1) at
US$0. Prices come from settings (`EXTRACT_PRICE_INPUT_PER_MTOK`,
`EXTRACT_PRICE_OUTPUT_PER_MTOK`), never from code; with either unset every cost is None. The
estimate uses the Phase 2 per-page averages (`EXTRACT_EST_INPUT_TOKENS_PER_PAGE`,
`EXTRACT_EST_OUTPUT_TOKENS_PER_PAGE`: both reads of a page together). Amounts are Decimal,
rounded half up to 0.001 (a tenth of a cent).
"""

from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal

from app.core.config import Settings
from app.domain.models import Extraction

USD_STEP = Decimal("0.001")
_MTOK = Decimal(1_000_000)


def tokens_cost(input_tokens: int, output_tokens: int, settings: Settings) -> Decimal | None:
    """US$ of `input_tokens` and `output_tokens` at the settings prices; None if either is unset."""
    price_in = settings.EXTRACT_PRICE_INPUT_PER_MTOK
    price_out = settings.EXTRACT_PRICE_OUTPUT_PER_MTOK
    if price_in is None or price_out is None:
        return None
    usd = (input_tokens * price_in + output_tokens * price_out) / _MTOK
    return usd.quantize(USD_STEP, rounding=ROUND_HALF_UP)


def estimate_read_cost(new_pages: int, settings: Settings) -> Decimal | None:
    """Estimated US$ of reading `new_pages` pages (both reads each) before any is read."""
    return tokens_cost(
        new_pages * settings.EXTRACT_EST_INPUT_TOKENS_PER_PAGE,
        new_pages * settings.EXTRACT_EST_OUTPUT_TOKENS_PER_PAGE,
        settings,
    )


def read_cost_of(extractions: Iterable[Extraction], settings: Settings) -> Decimal | None:
    """Actual US$ of stored reads: their recorded input and output tokens at the prices. Only
    the stored (successful) attempt of a read is recorded, so failed attempts are not counted."""
    reads = list(extractions)
    return tokens_cost(
        sum(e.input_tokens for e in reads), sum(e.output_tokens for e in reads), settings
    )
