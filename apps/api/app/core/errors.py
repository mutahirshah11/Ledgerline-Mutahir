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
    """G7: a backend service still failed after retries."""

    def __init__(self, service: str, detail: str) -> None:
        super().__init__(f"{service} failed: {detail}")
        self.service = service
        self.detail = detail
