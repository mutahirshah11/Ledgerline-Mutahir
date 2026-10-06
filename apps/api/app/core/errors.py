"""Domain errors raised by the Repo and BlobStore (contracts/repo-interface.md G4 to G7)."""

from typing import Any


class LedgerlineError(Exception):
    """Base for every domain error."""


class NotFound(LedgerlineError):
    """G4: an unknown id was requested."""

    def __init__(self, entity: str, id: object) -> None:
        super().__init__(f"{entity} not found: {id}")
        self.entity = entity
        self.id = id


class UniqueViolation(LedgerlineError):
    """G5: a unique key already exists."""

    def __init__(self, constraint: str) -> None:
        super().__init__(f"unique constraint violated: {constraint}")
        self.constraint = constraint


class StateConflict(LedgerlineError):
    """G6: a compare-and-set precondition failed; carries the current record."""

    def __init__(self, current: Any) -> None:
        super().__init__("state conflict")
        self.current = current


class AppendOnlyViolation(LedgerlineError):
    """An update or delete was attempted on an append-only record."""

    def __init__(self, entity: str) -> None:
        super().__init__(f"{entity} is append-only")
        self.entity = entity


class UpstreamFailed(LedgerlineError):
    """G7: a backend service still failed after retries.

    `stage` and `page_no` (003 E7) feed the alert text "Stage: extract (page 2)".
    """

    def __init__(
        self,
        service: str,
        detail: str,
        *,
        stage: str | None = None,
        page_no: int | None = None,
    ) -> None:
        super().__init__(f"{service} failed: {detail}")
        self.service = service
        self.detail = detail
        self.stage = stage
        self.page_no = page_no


class PageUnreadable(LedgerlineError):
    """003 E4 (FM-06): more than half of a page's merged cells are illegible."""

    def __init__(self, page_no: int, illegible: int, total: int) -> None:
        super().__init__(f"Page {page_no} could not be read, retake the photo")
        self.page_no = page_no
        self.illegible = illegible
        self.total = total


class InvalidReadOutput(LedgerlineError):
    """003 E5 (FM-05): a model answer that is not valid read JSON. Retryable; internal only."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"invalid read output: {detail}")
        self.detail = detail
