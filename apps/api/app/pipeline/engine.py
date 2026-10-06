"""Engine entry point (contracts/engine.md). Only `EngineInputs` so far; `run_pipeline` and
`reconcile_run` follow in T078."""

from dataclasses import dataclass
from uuid import UUID

from app.domain.models import RegisterPageInput, ReviewItem
from app.pipeline.normalise import MappingTables


@dataclass(frozen=True)
class EngineInputs:
    """Everything one reconcile reads (data-model.md Section 2)."""

    run_id: UUID
    month: str  # "YYYY-MM"
    register_pages: list[RegisterPageInput]
    erp_bytes: bytes
    sheet_bytes: bytes
    mappings: MappingTables
    decisions: list[ReviewItem]  # all review items of the run (any status)
